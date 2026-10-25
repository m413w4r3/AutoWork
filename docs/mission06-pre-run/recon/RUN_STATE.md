# M06 run-state reconnaissance

**Scope and evidence rule.** This is a docs-and-source audit at the supplied base `505c1cd2c525744bee5d768cccc1a830abce2d2b`. No live database, object store, container, provider, or private run archive was read. Every operational fact below is tagged `VERIFIED` with a repository path and line, or `UNKNOWN`; historical snapshots are not treated as current state. The retained run record says its detailed run journals and Bridge archives are in ignored local paths, so they are outside this audit ([docs/operations/run12-editorial-closure-2026-10-22.md:52](../../docs/operations/run12-editorial-closure-2026-10-22.md:52), [docs/operations/project-state-after-rerun-2026-10-20.md:5-8](../../docs/operations/project-state-after-rerun-2026-10-20.md:5)).

## 1. Last real operational run

- **VERIFIED:** The latest retained record that explicitly calls itself a real rerun is Run 12, dated **2026-10-19 to 2026-10-20**, with documented starting reference `main@77a276e` ([docs/operations/project-state-after-rerun-2026-10-20.md:1-4](../../docs/operations/project-state-after-rerun-2026-10-20.md:1)). This source gives only an abbreviated base commit.
- **VERIFIED:** The 2026-10-19 operational state maps “Investigation 12” `5f66ebfb-1653-45e6-83cd-a29af6f87868` to “run 12” `ec7a6807-ec96-4012-864a-6ca033faa951`, subject `d22822d4-27ce-47d2-9cc5-f84cf312986a`, edition `11f62c2b-5174-42ab-bf9f-9f4a524741e9` ([docs/operations/project-state-2026-10-19.md:27-29](../../docs/operations/project-state-2026-10-19.md:27)).
- **UNKNOWN:** The 2026-10-20 rerun report does not repeat those UUIDs or explicitly bind its dated rerun to that exact run/investigation row. The association is plausible from the “Run 12” label but is not asserted here as a verified row-level join ([docs/operations/project-state-after-rerun-2026-10-20.md:1-8](../../docs/operations/project-state-after-rerun-2026-10-20.md:1); [docs/operations/project-state-2026-10-19.md:27-29](../../docs/operations/project-state-2026-10-19.md:27)).
- **VERIFIED:** The subject is **Blinder Tunnel — campaign of espionage against Iraqi critical infrastructure via fake recruitment**, subject UUID `d22822d4-27ce-47d2-9cc5-f84cf312986a`; the recorded edition is `11f62c2b-5174-42ab-bf9f-9f4a524741e9`; its initial record gives TLP CLEAR, Subject version 1, and Unit 42’s 2026-10-06 article as primary source ([docs/operations/first-run-operator-journal-2026-10-16.md:9-14](../../docs/operations/first-run-operator-journal-2026-10-16.md:9)).
- **VERIFIED:** The rerun report records five `make up` redeployments without reset and no active job or invocation at each check; it also says no G2/G3 action, publication, repin, upload, rescan, Retrohunt, C2 contact, or sample execution occurred in that recorded session ([docs/operations/project-state-after-rerun-2026-10-20.md:18-20](../../docs/operations/project-state-after-rerun-2026-10-20.md:18)). This is a dated observation, not current readiness.
- **VERIFIED:** The later Run12 closure stopped on 2026-10-22 and records its starting reference as `main@2317440efba1c70b17870c56b6b23475a601239e` ([docs/operations/run12-editorial-closure-2026-10-22.md:1-5](../../docs/operations/run12-editorial-closure-2026-10-22.md:1)).
- **UNKNOWN:** Whether `main@2317440…` is a later code base for the same operational run, and the full SHA corresponding to `77a276e`, are not established by the retained records. Treat these as two differently scoped references, not as a resolved base-commit identity ([docs/operations/project-state-after-rerun-2026-10-20.md:3-4](../../docs/operations/project-state-after-rerun-2026-10-20.md:3); [docs/operations/run12-editorial-closure-2026-10-22.md:3](../../docs/operations/run12-editorial-closure-2026-10-22.md:3)).
- **UNKNOWN:** Run 12’s exact `synthesis_artifact_id`, synthesis version, canonical SHA-256, and canonical blob reference are absent from the retained Run12 records. The latest editorial closure instead records a **draft opinion v61** with SHA-256 `82a1c86218ccb7759669a20f3001bd1c45f5683cec5dabbb1d7f34911428ee49`; the opinion ID is not given there ([docs/operations/run12-editorial-closure-2026-10-22.md:16-17](../../docs/operations/run12-editorial-closure-2026-10-22.md:16)).
- **VERIFIED:** A separately documented earlier production run is `a6d09c1d-c2bd-4dac-9d7b-5f59c1f327ec` (batch `ea7e732c-e3c7-4de1-a2cc-372203ae7bbe`). It produced verified synthesis artifact `64baa901-4b35-4ffd-97c5-3ed1a3ec74eb`, version 1, SHA-256 `8eb9cbe0fade4cffe00fb0f5f645a5f2dd074ce431e07c16a0e487a205bdd618`, and V5 publication artifact `114d0130-19ec-4761-8576-967ee6882648`, version 1, SHA-256 `5cf74aa7feda9fbbc1b0ea1cf9f9913032c266b18f966f89ae55add6a75367c8` ([docs/operations/first-run-operator-journal-2026-10-16.md:88-97](../../docs/operations/first-run-operator-journal-2026-10-16.md:88)). These are not established as Run 12’s exact synthesis/publication.
- **VERIFIED:** That earlier run’s extraction artifact is `495fc542-74f8-4065-8499-094c6d594fd5`, version 1; its document gives SHA prefix `d08dba02…`, not the full hash ([docs/operations/first-run-pivot-plan-2026-10-16.md:1-3](../../docs/operations/first-run-pivot-plan-2026-10-16.md:1); [docs/operations/first-run-operator-journal-2026-10-16.md:91](../../docs/operations/first-run-operator-journal-2026-10-16.md:91)). Full extraction SHA-256: **UNKNOWN**.
- **VERIFIED:** An earlier 2026-10-19 snapshot records opinion `d9935324-2ccc-5067-9dc9-17efc0637823`, v14 draft, SHA-256 `8b182ca4f580982bb97f57730032e61585f084ac1ff645cd33ef65a86e6b3484`; PDF preview SHA-256 `de07198ac6ae9384a575fda97a849cb65840fb9a5e2f9db85d9375c21b218ca7`; protocol-model blob `e3b0d242-43ca-4b17-a406-b0a97a731acd`; render `fef52fde-e1f9-506f-977d-35426fca3c00` ([docs/operations/project-state-2026-10-19.md:36-40](../../docs/operations/project-state-2026-10-19.md:36)). Whether this v14 opinion is the lineage that later became v61 is **UNKNOWN**.

## 2. Prior investigations on the subject

| Record | Documented state and limits |
|---|---|
| Initial pre-investigation snapshot, 2026-10-16 | **VERIFIED:** No investigation existed at the first read; the initial production run was `a6d09c1d-c2bd-4dac-9d7b-5f59c1f327ec`. **UNKNOWN:** current state is not implied by this snapshot ([docs/operations/first-run-operator-journal-2026-10-16.md:14-18](../../docs/operations/first-run-operator-journal-2026-10-16.md:14)). |
| Investigation `f0685708-e887-40f2-8a32-0075c083aa4f` | **VERIFIED:** Created against the initial run with job `0f1ab983-e4d3-44a5-af6f-3a3f7a64ebb2`; after its ambiguous turn was human-previewed and adopted by exact hash, it later completed `agent_finished` at v23 with 14 agent turns, 11 documentary qualified findings, 11 observations, and draft opinion v3 (SHA prefix `60a7326d…`). No hypotheses were recorded. **VERIFIED:** The 2026-10-16 report says VT/Shodan pivots were blocked pending G2 at that point; a later G2 approval was recorded with `do_not_submit=true`, which the report says blocked further Bridge submissions for that investigation. **UNKNOWN:** current persisted state ([docs/operations/first-run-operator-journal-2026-10-16.md:95](../../docs/operations/first-run-operator-journal-2026-10-16.md:95), [112-119](../../docs/operations/first-run-operator-journal-2026-10-16.md:112), [121-145](../../docs/operations/first-run-operator-journal-2026-10-16.md:121), [127-145](../../docs/operations/first-run-operator-journal-2026-10-16.md:127)). |
| Investigation 12 `5f66ebfb-1653-45e6-83cd-a29af6f87868` | **VERIFIED:** Associated in the 2026-10-19 record with Run 12 `ec7a6807-ec96-4012-864a-6ca033faa951` and this subject. The correction-state report says it was `running`, “not touched,” at that snapshot. **UNKNOWN:** its state after that point and its exact synthesis artifact/hash ([docs/operations/project-state-2026-10-19.md:27-29](../../docs/operations/project-state-2026-10-19.md:27); [docs/operations/correction-state-2026-10-19.md:7-10](../../docs/operations/correction-state-2026-10-19.md:7)). |

**VERIFIED:** The later 2026-10-20 redeploy checks found no active jobs or invocations at each check, but do not provide a final state/version for Investigation 12 ([docs/operations/project-state-after-rerun-2026-10-20.md:18](../../docs/operations/project-state-after-rerun-2026-10-20.md:18)). **UNKNOWN:** Current status of either prior investigation and whether any additional investigation exists for the subject.

## 3. Evidence, samples, versions, and storage references

### Acquired samples

- **VERIFIED:** The 2026-10-19 state report documents four VirusTotal-downloaded samples in `samples-quarantine`, says their SHA-256 values were verified, and says later investigations of the same subject reused them without downloading again ([docs/operations/project-state-2026-10-19.md:20-22](../../docs/operations/project-state-2026-10-19.md:20)).
- **VERIFIED:** Documented sample names, sizes, and only the SHA prefixes explicitly attached to those names are:

| Sample | Size | Documented SHA-256 prefix |
|---|---:|---|
| `RuntimeBroker.dll` | 33,280 bytes | `53f35e49…` |
| `PsProxy.dll` | 6,144 bytes | `3fd810a3…` |
| `Blackwood.dll` | 8,487,424 bytes | `76273382…` |
| ZIP archive | 134,661 bytes | `6e7d9b33…` |

  Citation: [docs/operations/project-state-2026-10-19.md:20-22](../../docs/operations/project-state-2026-10-19.md:20).
- **VERIFIED:** The extraction pivot plan separately lists full hash strings with those four prefixes, among 17 values under its “Hashes” heading ([docs/operations/first-run-pivot-plan-2026-10-16.md:5-22](../../docs/operations/first-run-pivot-plan-2026-10-16.md:5)). **UNKNOWN:** The retained documents do not explicitly bind each full pivot-plan value to a named quarantined sample row, so the full sample SHA values remain UNKNOWN pending canonical row inspection.
- **UNKNOWN:** Sample UUIDs, sample-record versions, per-investigation association IDs/qualification histories, acquisition-attempt IDs, MinIO blob IDs, object keys, and current object availability are not documented. The only documented storage locator is `samples-quarantine`; its live resolution is UNKNOWN ([docs/operations/project-state-2026-10-19.md:20-22](../../docs/operations/project-state-2026-10-19.md:20); schema fields: [backend/src/cti_app/infrastructure/database/models/core.py:27-47](../../backend/src/cti_app/infrastructure/database/models/core.py:27), [backend/src/cti_app/infrastructure/database/models/core.py:288-345](../../backend/src/cti_app/infrastructure/database/models/core.py:288)).

### Canonical artifacts and observations

- **VERIFIED:** The initial run’s artifact list is: references `8e025f3c…` v1; extraction `495fc542-74f8-4065-8499-094c6d594fd5` v1, SHA prefix `d08dba02…`; relevance projection `a5e75d66…`; synthesis `64baa901-4b35-4ffd-97c5-3ed1a3ec74eb` v1, SHA-256 `8eb9cbe0fade4cffe00fb0f5f645a5f2dd074ce431e07c16a0e487a205bdd618`; editorial enrichment `db912ed5…`; publication V5 `114d0130-19ec-4761-8576-967ee6882648` v1, SHA-256 `5cf74aa7feda9fbbc1b0ea1cf9f9913032c266b18f966f89ae55add6a75367c8`; TLP CLEAR ([docs/operations/first-run-operator-journal-2026-10-16.md:88-92](../../docs/operations/first-run-operator-journal-2026-10-16.md:88)). Full IDs/hashes for abbreviated references and enrichment, plus whether these artifacts are the exact Run12 lineage: **UNKNOWN**.
- **VERIFIED:** The extraction pivot plan contains 17 hash values, 9 IPv4 indicators, 8 domain indicators, and 5 URLs, sourced from extraction artifact `495fc542-74f8-4065-8499-094c6d594fd5` ([docs/operations/first-run-pivot-plan-2026-10-16.md:1-3](../../docs/operations/first-run-pivot-plan-2026-10-16.md:1), [5-50](../../docs/operations/first-run-pivot-plan-2026-10-16.md:5)). These are documented IOC/pivot inputs; they are not proof that bytes were acquired for each hash.
- **VERIFIED:** Four VT behavior PCAP sizes and packet counts are recorded: RuntimeBroker 7,807 bytes / 52 packets; Blackwood 1,219 / 8; ZIP 2,181 / 16; PsProxy 24 / 0 ([docs/operations/project-state-2026-10-19.md:44-46](../../docs/operations/project-state-2026-10-19.md:44)). Their individual SHA-256 values, blob IDs, MinIO keys, and versions are **UNKNOWN**. The same record says the operator had not yet read these captures after the TShark fix, and Suricata replay/flow/protocol work remained to do ([docs/operations/project-state-2026-10-19.md:44-46](../../docs/operations/project-state-2026-10-19.md:44)).
- **VERIFIED:** A 2026-10-19 DDL record reports aggregate database counts immediately before/after its additive change: 12 investigations, 139 finding versions, 340 finding observations, 184 finding claims, 1,290 evidence observations, 995 tool invocations, and 997 job events; both C2 experiment tables had zero rows. These are database-wide counts at that historical check, not counts for Investigation 12 alone ([docs/operations/c2-experiments-ddl-2026-10-19.md:78-89](../../docs/operations/c2-experiments-ddl-2026-10-19.md:78)).
- **VERIFIED:** The later Run12 closure reports real archived Bridge inputs C14/C15; their canonical context sizes were 177,275 and 186,187 bytes, and the full inputs were 199,592 and 213,400 bytes. Their diagnostic renders were 157,108 and 167,422 bytes, both over the 96,000-byte renderer limit. Semantic compaction and replay remained outstanding ([docs/operations/run12-editorial-closure-2026-10-22.md:14-15](../../docs/operations/run12-editorial-closure-2026-10-22.md:14)). The archive file paths are only documented as local/private and are not inspected here ([docs/operations/run12-editorial-closure-2026-10-22.md:52](../../docs/operations/run12-editorial-closure-2026-10-22.md:52)).

## 4. Analyses, jobs, and reconciliation work

- **VERIFIED:** On 2026-10-19, Investigation 12 was recorded as `running` and untouched; the same report says C1 pivots, C2 experiments, C4 network, C5 advisor, C7 UI, C8 Typst, and C9 context did not yet have real-data proof, with C3/C6 only partial ([docs/operations/correction-state-2026-10-19.md:9](../../docs/operations/correction-state-2026-10-19.md:9), [30-39](../../docs/operations/correction-state-2026-10-19.md:30)).
- **VERIFIED:** Later that date, the C2 DDL record says there were 0 `RUNNING` and 0 `QUEUED` jobs before applying the DDL; a GET of Investigation 12 returned HTTP 200 with stream version 0 and empty experiment/claim views ([docs/operations/c2-experiments-ddl-2026-10-19.md:78-89](../../docs/operations/c2-experiments-ddl-2026-10-19.md:78)). The 2026-10-20 report separately records no active job/invocation at each of five redeploy checks ([docs/operations/project-state-after-rerun-2026-10-20.md:18](../../docs/operations/project-state-after-rerun-2026-10-20.md:18)). Current job state: **UNKNOWN**.
- **VERIFIED:** The 2026-10-22 closure records a Bridge invocation `da013050-6d13-49c9-9388-9b1bc75f74e8` whose admission refused a `tool_seconds` reservation (266 seconds available versus 3,600 reserved by default); it did not reach Bridge transport and produced no Bridge opinion. This is documented as not transmitted, not as an ambiguous send ([docs/operations/run12-editorial-closure-2026-10-22.md:11-13](../../docs/operations/run12-editorial-closure-2026-10-22.md:11)).
- **VERIFIED:** The closure explicitly leaves reconciliation of `needs_review` invocations for human review against persisted state ([docs/operations/run12-editorial-closure-2026-10-22.md:36-41](../../docs/operations/run12-editorial-closure-2026-10-22.md:36)). Exact invocation IDs/counts and current statuses: **UNKNOWN**.
- **VERIFIED:** Investigation `f0685708-e887-40f2-8a32-0075c083aa4f` had an ambiguous Bridge turn; the recorded human preview/adoption matched the candidate SHA `3d677113…` and completed the turn without replaying that Bridge call ([docs/operations/first-run-operator-journal-2026-10-16.md:107-124](../../docs/operations/first-run-operator-journal-2026-10-16.md:107)). The same record later reports the investigation completed at v23 ([docs/operations/first-run-operator-journal-2026-10-16.md:127-133](../../docs/operations/first-run-operator-journal-2026-10-16.md:127)).
- **VERIFIED:** The 2026-10-20 run-state report lists open CORR-031 (protocol figures in PDF v27), CORR-030 (incorrect playbook schema for `network.protocol.render`), CORR-027 (`args_hint` cleanup not replayable without another Bridge call), missing .NET strings/resources from the pivot frontier, and a missing independent critical read-only audit. It reports 16/22 major claims with referenced verdicts and all four PCAPs as background DNS only, leaving C2 protocol `not_testable`; opinion v27 remained draft ([docs/operations/project-state-after-rerun-2026-10-20.md:44-56](../../docs/operations/project-state-after-rerun-2026-10-20.md:44)).
- **VERIFIED:** The 2026-10-22 closure says opinion v61 remained draft; reviews with new dossiers, a token-length correction, a new version, PDF page QA, and an independent final audit had not been completed. YARA remained `ONE_RULE_NOT_VALIDATED`; Suricata was `SURICATA_NOT_JUSTIFIED` ([docs/operations/run12-editorial-closure-2026-10-22.md:16-20](../../docs/operations/run12-editorial-closure-2026-10-22.md:16), [36-50](../../docs/operations/run12-editorial-closure-2026-10-22.md:36)). Whether v27 and v61 are versions of the same opinion: **UNKNOWN**.

## 5. Resume capabilities and readiness caveats

- **VERIFIED:** The API accepts a verified synthesis handoff only when the run belongs to the subject, the synthesis artifact exists, is `VERIFIED`, is stage `synthesis`, belongs to that same run and subject, and its canonical snapshot SHA/identity matches the submitted values ([backend/src/cti_app/api/investigation.py:825-848](../../backend/src/cti_app/api/investigation.py:825), [2641-2654](../../backend/src/cti_app/api/investigation.py:2641)).
- **VERIFIED:** A second investigation cannot be created on the same production run: the model has a unique constraint on `analyst_investigations.production_run_id`; a repeated start for the same run/artifact reuses that run’s handoff, and a different artifact on that run conflicts ([backend/src/cti_app/infrastructure/database/models/production.py:336-340](../../backend/src/cti_app/infrastructure/database/models/production.py:336), [backend/src/cti_app/api/investigation.py:917-924](../../backend/src/cti_app/api/investigation.py:917), [docs/investigation_api.md:14-17](../../docs/investigation_api.md:14)).
- **VERIFIED:** A distinct investigation therefore requires a distinct production run and a verified synthesis artifact owned by that run. **UNKNOWN / blocking precondition:** the retained docs do not prove that an identical frozen synthesis can be bound as a new run-owned artifact without recomputing or changing its lineage; do not assume cross-run reuse is supported ([backend/src/cti_app/api/investigation.py:2641-2654](../../backend/src/cti_app/api/investigation.py:2641)).
- **VERIFIED:** A matching subject sample already in quarantine can be resolved by hash; the code verifies the quarantine blob SHA and can record subject-sample reuse with `new_download: false` and provenance `investigation.subject_sample_reuse` ([backend/src/cti_app/application/investigation_acquisition.py:554-622](../../backend/src/cti_app/application/investigation_acquisition.py:554), [681-763](../../backend/src/cti_app/application/investigation_acquisition.py:681)). **UNKNOWN:** current sample rows, exact association creation/reuse path, MinIO object presence/readability, and runtime worker readiness.
- **VERIFIED:** Without persisted G2 approval, article and local tools on samples already in quarantine may be used; external provider calls and acquisition are denied. G2 is human, audited, budgeted, and bounded by source policy; G3 sample qualification is a separate human append-only decision ([docs/investigation_api.md:42-61](../../docs/investigation_api.md:42)). The available tool list depends on current configured ports, archives, and capability flags; a configured key alone does not establish availability ([docs/investigation_operations.md:3-8](../../docs/investigation_operations.md:3), [47-55](../../docs/investigation_operations.md:47)). **UNKNOWN:** current G2, source policy, budgets, enabled capabilities, and authenticated identity.
- **VERIFIED:** Durable investigation jobs use idempotent generations; job recovery retries the same attempt, replays deterministic persisted results, and sends ambiguous submissions to review without resending. The documented offline PostgreSQL recovery oracle passed for ambiguous-send and proven-no-send classes, but its framework tool-invocation branch was `NOT_EXERCISED` ([docs/investigation_api.md:79-88](../../docs/investigation_api.md:79), [docs/mission05-phase3/FINAL_AUDIT_AND_HANDOFF.md:99-121](../../docs/mission05-phase3/FINAL_AUDIT_AND_HANDOFF.md:99)). **UNKNOWN:** whether any live job now needs recovery.
- **VERIFIED:** Mission 05 and the final Run12 handoff retain `S3_BLOCKED_DESIGN`; MinIO/S3 was not exercised and no storage readiness was demonstrated ([docs/mission05-phase3/FINAL_AUDIT_AND_HANDOFF.md:121](../../docs/mission05-phase3/FINAL_AUDIT_AND_HANDOFF.md:121), [191-196](../../docs/mission05-phase3/FINAL_AUDIT_AND_HANDOFF.md:191), [docs/mission05-phase3/SECURITY_REPLAY_AND_S3_GATES.md:15-17](../../docs/mission05-phase3/SECURITY_REPLAY_AND_S3_GATES.md:15)). **UNKNOWN:** live MinIO/S3 configuration, current objects, and current storage health.
- **UNKNOWN:** No built-in independent observer/defect-log capability is established by the scoped route and operations docs. The restart plan below assigns this as a human read-only role, not as a product feature ([docs/investigation_api.md:10-25](../../docs/investigation_api.md:10), [docs/investigation_operations.md:57-75](../../docs/investigation_operations.md:57)).

## 6. Draft skeleton for `RUN_RESTART_PLAN.md`

> **DRAFT ONLY — no operational action authorized by this report.**

### Objective and lineage lock

- **VERIFIED:** Subject: Blinder Tunnel, `d22822d4-27ce-47d2-9cc5-f84cf312986a` ([docs/operations/first-run-operator-journal-2026-10-16.md:9-14](../../docs/operations/first-run-operator-journal-2026-10-16.md:9)).
- **UNKNOWN / must be filled from authorized canonical read:** exact frozen synthesis `(production_run_id, synthesis_artifact_id, version, canonical SHA-256, canonical blob id)` for the proposed new investigation. Do not substitute the initial run’s synthesis or the v61 opinion hash for this missing lineage.
- **Precondition:** an owner-approved lineage must identify a distinct production run whose own synthesis artifact is `VERIFIED`, belongs to that run and subject, and has a canonical digest matching the explicitly approved frozen content. If the current implementation cannot represent that lineage, stop and request a design decision; do not create a duplicate on an old run ([backend/src/cti_app/api/investigation.py:831-848](../../backend/src/cti_app/api/investigation.py:831), [917-924](../../backend/src/cti_app/api/investigation.py:917)).

### Preconditions before any run action

1. **Canonical read:** obtain all subject production runs, synthesis artifacts, investigations, and their exact statuses/version/hash; verify the proposed distinct run/artifact tuple and source snapshot. Record the DB and app schema version.
2. **No hidden state assumption:** read current statuses for previous investigations, durable jobs, model runs/turns, tool invocations, budget reservations, and `needs_review`; reconcile ambiguous outcomes through the documented human CAS route before any resume or new external action ([docs/investigation_api.md:68-77](../../docs/investigation_api.md:68)).
3. **Evidence preservation:** enumerate existing subject sample hashes, quarantine blob references, evidence observations, acquisition attempts, and sample qualification history. Confirm matching bytes by the canonical SHA before reuse. Never delete, overwrite, or reacquire an existing sample solely to make the new investigation work ([backend/src/cti_app/application/investigation_acquisition.py:554-622](../../backend/src/cti_app/application/investigation_acquisition.py:554), [681-763](../../backend/src/cti_app/application/investigation_acquisition.py:681)).
4. **Policy and capability gate:** read the frozen synthesis/input-snapshot TLP and disclosure fields; inspect the current available-tool catalogue, G2 envelope/version, budgets, worker attestation/confinement, and authenticated operator identity. If no suitable human G2 approval exists, limit work to allowed local reads and quarantined-sample analysis ([docs/investigation_api.md:42-56](../../docs/investigation_api.md:42), [docs/investigation_operations.md:88-105](../../docs/investigation_operations.md:88)).
5. **Storage gate:** prove the exact registered blob refs resolve in the configured canonical store and that a read-only content check reproduces each recorded SHA. S3 remains blocked until its separate design/runtime gates are satisfied ([docs/mission05-phase3/FINAL_AUDIT_AND_HANDOFF.md:191-196](../../docs/mission05-phase3/FINAL_AUDIT_AND_HANDOFF.md:191)).
6. **Independent observer:** appoint a human observer with read-only access to the run. The observer records timestamp/checkpoint, request or job ID, source version/hash, observed defect, and evidence reference in a separate append-only run log; the observer does not silently correct inputs, edit canonical records, retry, or authorize external effects. Any correction is a separately reviewed and explicitly authorized action.
7. **Approval gate:** before starting any production run or investigation, obtain explicit authorization for the exact action, owner, source lineage, external scope, G2/G3 decisions, budgets, and idempotency key. This reconnaissance itself authorizes none.

### Execution record (fill during an authorized future run)

- New production run ID / run number / subject / run-owned synthesis artifact ID and version / SHA-256: `UNKNOWN — pending authorized action`.
- New investigation ID / job ID / expected version / idempotency key reference: `UNKNOWN — pending authorized action`.
- Reused sample association IDs / verified SHA-256 / existing blob IDs / reuse provenance: `UNKNOWN — pending canonical reads`; no download requested.
- Observer checkpoints and defect ledger references: `UNKNOWN — pending future run`.
- Stop criteria: any lineage mismatch, missing/corrupt blob, unresolved `needs_review`, stale CAS version, unavailable required capability, policy denial, or unauthorized side effect. Stop and record; do not retry or repair silently.

### Explicit non-actions

- No deleting, resetting, overwriting, or restamping earlier investigations, findings, observations, artifacts, opinions, jobs, samples, or blobs.
- No downloading/reacquiring any already documented sample; no new sample acquisition, rescan, Retrohunt, malware execution, C2 contact, or upload.
- No Bridge/VT/Shodan/provider call without the separate required authorization and current G2/disclosure checks.
- No G2/G3 approval, envelope extension/revocation, opinion approval, publication, release, repin, migration, or S3 cutover under this draft.

## 7. Gaps that only authorized live reads can close

- **UNKNOWN:** whether Run12 UUIDs `5f66…` / `ec7a…` are the exact rows represented by the 2026-10-20 rerun, and what the current production and investigation states are.
- **UNKNOWN:** exact Run12 synthesis artifact ID/version/SHA/canonical blob and whether a separate verified run can carry the approved frozen content under current lineage rules.
- **UNKNOWN:** current subject investigation list and statuses; current jobs, heartbeats, attempts, model run submission states, Bridge turns, tool invocation states, ambiguous outcomes, and `needs_review` count/IDs.
- **UNKNOWN:** current G2 envelope/version/provider permissions/disclosure, revocations, budget reservations/remaining amounts, current TLP and source restrictions, authenticated identity, and actual advertised tools.
- **UNKNOWN:** full sample SHA-to-name mapping, sample IDs/states, per-investigation associations and G3 histories, acquisition ledger, blob IDs/keys, and whether each quarantined object is readable and hash-correct in MinIO.
- **UNKNOWN:** current canonical evidence-observation/artifact counts and hashes for Investigation 12; the 1,290 observation count is database-wide and historical.
- **UNKNOWN:** latest opinion ID/lineage for v61, its canonical artifact ref, and current PDF/version/QA state.
- **UNKNOWN:** live S3/MinIO configuration, object existence, and runtime health. Mission 05’s S3 result is a design block, not a live readiness proof.

### Exact read-only PostgreSQL inventory query

**UNKNOWN until an authorized operator runs it.** This query is designed from the current ORM table/column definitions; it does not read blob bytes or MinIO. Run against a read-only role in a read-only transaction. Do not paste object keys or evidence values into unrelated logs.

```sql
BEGIN TRANSACTION READ ONLY;

SELECT version_num FROM alembic_version;

-- Subject run, investigation, and every run-owned synthesis artifact.
SELECT
    pr.id AS production_run_id,
    pr.run_number,
    pr.status AS production_status,
    pr.current_stage AS production_stage,
    pr.version AS production_version,
    ai.id AS investigation_id,
    ai.status AS investigation_status,
    ai.current_stage AS investigation_stage,
    ai.cycle_number,
    ai.version AS investigation_version,
    pa.id AS synthesis_artifact_id,
    pa.version AS synthesis_version,
    pa.status AS synthesis_status,
    pa.input_hash,
    pa.canonical_blob_id,
    b.sha256 AS canonical_blob_sha256,
    b.size AS canonical_blob_size,
    b.logical_bucket AS canonical_blob_bucket,
    b.object_key AS canonical_blob_key
FROM production_runs AS pr
LEFT JOIN analyst_investigations AS ai ON ai.production_run_id = pr.id
LEFT JOIN production_artifacts AS pa
    ON pa.production_run_id = pr.id AND pa.stage = 'synthesis'
LEFT JOIN blobs AS b ON b.id = pa.canonical_blob_id
WHERE pr.subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
ORDER BY pr.run_number, pa.version, ai.id;

-- Subject samples and canonical blob metadata; no bytes returned.
SELECT
    s.id AS sample_id,
    s.original_name,
    s.origin,
    s.origin_kind,
    s.state,
    s.tlp,
    s.do_not_submit,
    s.external_llm_allowed,
    s.blob_id,
    b.sha256,
    b.size,
    b.logical_bucket,
    b.object_key
FROM samples AS s
JOIN blobs AS b ON b.id = s.blob_id
WHERE s.subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
ORDER BY b.sha256, s.id;

-- Per-investigation sample associations and latest qualification.
SELECT
    i.id AS investigation_id,
    sa.id AS association_id,
    sa.sample_id,
    sa.sample_sha256,
    sa.origin,
    sv.version AS qualification_version,
    sv.qualification
FROM analyst_investigations AS i
JOIN sample_associations AS sa ON sa.investigation_id = i.id
LEFT JOIN LATERAL (
    SELECT v.version, v.qualification
    FROM sample_association_versions AS v
    WHERE v.association_id = sa.id
    ORDER BY v.version DESC
    LIMIT 1
) AS sv ON TRUE
WHERE i.subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
ORDER BY i.id, sa.sample_sha256;

-- Canonical evidence metadata only; omit source text and raw provider payloads.
SELECT
    eo.investigation_id,
    eo.id AS evidence_observation_id,
    eo.kind,
    eo.sha256,
    eo.source_version,
    eo.blob_id,
    b.logical_bucket,
    b.size
FROM evidence_observations AS eo
JOIN analyst_investigations AS i ON i.id = eo.investigation_id
LEFT JOIN blobs AS b ON b.id = eo.blob_id
WHERE i.subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
ORDER BY eo.investigation_id, eo.observed_at, eo.id;

-- Durable investigation and production jobs, including terminal rows.
SELECT
    j.id AS job_id,
    j.kind,
    j.aggregate_type,
    j.aggregate_id,
    j.status,
    j.attempt,
    j.max_attempts,
    j.heartbeat_at,
    j.error_code
FROM jobs AS j
WHERE j.aggregate_id IN (
    SELECT id FROM analyst_investigations
    WHERE subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
)
OR j.aggregate_id IN (
    SELECT id FROM production_runs
    WHERE subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
)
ORDER BY j.created_at, j.id;

-- Persisted loop version, turn, and stop reason.
SELECT
    i.id AS investigation_id,
    i.status AS investigation_status,
    ws.version AS state_version,
    ws.turn_index,
    ws.stop_reason,
    ws.updated_at AS state_updated_at
FROM analyst_investigations AS i
LEFT JOIN investigation_working_states AS ws ON ws.investigation_id = i.id
WHERE i.subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
ORDER BY i.id;

-- Tool invocation status, without payloads or raw response refs.
SELECT
    ti.investigation_id,
    ti.id AS invocation_id,
    ti.tool_name,
    ti.status,
    ti.version,
    ti.created_at,
    ti.updated_at
FROM tool_invocations AS ti
JOIN analyst_investigations AS i ON i.id = ti.investigation_id
WHERE i.subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
ORDER BY ti.investigation_id, ti.created_at, ti.id;

-- Historical acquisition attempts; no response payloads.
SELECT
    saa.investigation_id,
    saa.id AS acquisition_attempt_id,
    saa.requested_hash,
    saa.reason,
    saa.outcome,
    saa.sample_id,
    saa.error_code,
    saa.occurred_at
FROM sample_acquisition_attempts AS saa
JOIN analyst_investigations AS i ON i.id = saa.investigation_id
WHERE i.subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
ORDER BY saa.investigation_id, saa.occurred_at, saa.id;

-- Bridge/model turn transport state, without prompts or outputs.
SELECT
    i.id AS investigation_id,
    t.id AS turn_id,
    t.sequence,
    t.status AS turn_status,
    mr.id AS model_run_id,
    mr.status AS model_status,
    mr.submission_state,
    mr.submission_attempt,
    mr.bridge_request_id,
    mr.error_code
FROM analyst_investigations AS i
LEFT JOIN model_conversation_turns AS t
    ON t.conversation_id = i.pivot_conversation_id
LEFT JOIN model_runs AS mr ON mr.id = t.model_run_id
WHERE i.subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
ORDER BY i.id, t.sequence;

-- Human G2 envelope and current recorded policy/budget snapshot.
SELECT
    e.investigation_id,
    e.version,
    e.allowed_providers,
    e.budget_snapshot,
    e.acquire_hits_allowed,
    e.disclosure_tlp,
    e.disclosure_do_not_submit,
    e.disclosure_external_llm_allowed,
    e.updated_at
FROM investigation_envelopes AS e
JOIN analyst_investigations AS i ON i.id = e.investigation_id
WHERE i.subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
ORDER BY e.investigation_id;

-- Opinion identity, version, approval/draft status, and content digest.
SELECT
    ov.investigation_id,
    ov.opinion_id,
    ov.version,
    ov.status,
    ov.content_sha256,
    ov.created_at
FROM analyst_opinion_versions AS ov
JOIN analyst_investigations AS i ON i.id = ov.investigation_id
WHERE i.subject_id = 'd22822d4-27ce-47d2-9cc5-f84cf312986a'::uuid
ORDER BY ov.investigation_id, ov.opinion_id, ov.version;

COMMIT;
```

**VERIFIED schema basis:** table/column names above are present in the current ORM for `production_runs`/`production_artifacts`/`analyst_investigations` ([backend/src/cti_app/infrastructure/database/models/production.py:77-143](../../backend/src/cti_app/infrastructure/database/models/production.py:77), [146-214](../../backend/src/cti_app/infrastructure/database/models/production.py:146), [336-412](../../backend/src/cti_app/infrastructure/database/models/production.py:336)); `blobs` and `samples` ([backend/src/cti_app/infrastructure/database/models/core.py:27-47](../../backend/src/cti_app/infrastructure/database/models/core.py:27), [288-345](../../backend/src/cti_app/infrastructure/database/models/core.py:288)); evidence, sample association/history, and envelope tables ([backend/src/cti_app/infrastructure/database/models/investigation.py:104-150](../../backend/src/cti_app/infrastructure/database/models/investigation.py:104), [317-369](../../backend/src/cti_app/infrastructure/database/models/investigation.py:317), [547-582](../../backend/src/cti_app/infrastructure/database/models/investigation.py:547)); jobs ([backend/src/cti_app/infrastructure/database/models/jobs.py:23-67](../../backend/src/cti_app/infrastructure/database/models/jobs.py:23)); persisted working state and opinion versions ([backend/src/cti_app/infrastructure/database/models/investigation.py:511-544](../../backend/src/cti_app/infrastructure/database/models/investigation.py:511), [677-718](../../backend/src/cti_app/infrastructure/database/models/investigation.py:677)); sample acquisition ledger ([backend/src/cti_app/infrastructure/database/models/production.py:469-525](../../backend/src/cti_app/infrastructure/database/models/production.py:469)); model runs/turns ([backend/src/cti_app/infrastructure/database/models/model_execution.py:39-122](../../backend/src/cti_app/infrastructure/database/models/model_execution.py:39), [196-247](../../backend/src/cti_app/infrastructure/database/models/model_execution.py:196)).

**UNKNOWN after SQL:** a blob row does not prove the object exists or its bytes still hash correctly in MinIO. An authorized storage read must stat the exact bucket/key from the canonical blob row and, where integrity is needed, stream bytes read-only and recompute SHA-256. Current storage is not exercised by the cited S3 gates ([docs/mission05-phase3/SECURITY_REPLAY_AND_S3_GATES.md:7-17](../../docs/mission05-phase3/SECURITY_REPLAY_AND_S3_GATES.md:7)).
