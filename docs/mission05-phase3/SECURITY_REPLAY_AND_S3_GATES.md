# Mission 05 — security, replay and S3 gates

Status: **CLOSED for the offline phase** — populated from integrated lots and
the final gate replay. Any row not backed by an executed test is
`NOT_DEMONSTRATED`, never PASS. S3 and Advisor cutover remain blocked.

## Absolute boundaries observed

- No real Bridge/ChatGPT/provider/ModelGateway call. No VirusTotal, Shodan,
  Retrohunt, DNS, IOC or C2 contact. No acquisition, scan, upload, download or
  execution of malware. No publication/release/repin, no deployment, no live
  migration, no live PostgreSQL/MinIO access, no secret read.
- All E2E transports are local fakes; each refusal/veto must show
  `fake_send_count == 0` and no external HTTP client use.
- S3 remains `S3_BLOCKED_DESIGN`; Advisor cutover remains
  `ADVISOR_CUTOVER_BLOCKED_BY_S3`. No S3 migration, dispatcher replacement,
  instance-lock removal or routing change is in scope.

## D7 veto matrix (target: every veto yields zero fake sends)

| Veto / boundary | Expected typed refusal | Zero-send proof | Status |
|---|---|---|---|
| `TLP:RED` envelope/sample | disclosure refusal, no prepared dispatch | fake transport counter 0 | **PASS (M05-C + M05-D PG)** — `tlp_red` source on a persisted, confirmed association; transport calls 0 |
| `do_not_submit=true` | disclosure refusal | fake counter 0 | **PASS (M05-C + M05-D PG)** — `do_not_submit` source; transport calls 0 |
| `external_llm_allowed=false` | disclosure refusal | fake counter 0 | **PASS (M05-C + M05-D PG)** — `external_llm_allowed_false` source; transport calls 0 |
| sample/source/owner restriction | disclosure refusal naming association IDs/hashes, never restriction text | fake counter 0 | **PASS (M05-D PG)** — `license_restriction` source; trace carries association id, hash, source code and linked observation only, no restriction text; transport calls 0 |
| AutoWork operational credential in payload | redaction or `secret_field_remaining` / credential refusal | fake counter 0 | **PASS (M05-D PG)** — synthetic credential sentinel refused with `autowork_credential_or_signed_url_detected`; sentinel absent from the response; transport calls 0 |
| signed URL / revoked authorization | credential/signed-URL refusal | fake counter 0 | **PASS (M05-D PG)** — synthetic signed-URL sentinel refused; M05-C also exercises revoked disclosure; transport calls 0 |
| canonical evidence absent / completeness not READY | `evidence_preflight_*` refusal | fake counter 0 | **PASS (M05-C + M05-D PG)** — missing evidence ref yields `advisor_input_invalid`, `confirmed_non_execution=true`, `provider_usage={"status":"not_submitted"}`, transport calls 0 |
| ref identity conflict (same kind/id, two SHAs) | `ref_reference_conflict` | fake counter 0 | **PASS (M05-A unit)** — pure `prepare()` returns `PreparationRefusal` before any transport exists (`tests/test_advisor_consultation.py`); no PG E2E for this row |
| evidence reference absent or from another investigation | `advisor_input_invalid` / `advisor_input_rejected`, `confirmed_non_execution=true`, `provider_usage.not_submitted` | fake counter 0 | **PASS (M05-F unit + PG, M05-C/D PG)** — RED `UnboundLocalError` at base line 1006, GREEN typed refusal at candidate `a1b356c6` / integrated `12671c6a`; D duplicates it on integrated main |

## Replay / restart invariants

| Invariant | Expected | Status |
|---|---|---|
| Restart with `RUNNING` or uncertain external outcome | reservation and diagnostics preserved; no second call on the same operation key; `NEEDS_REVIEW` unless canonical bounded non-submission proof | **PASS for the two proven in-flight classes (closure, `4111acf3`)** — (a) *ambiguous submission*: durable job `RUNNING`, turn `RUNNING`, ModelRun `RUNNING`/`EXTERNAL_STATE_UNKNOWN` with a stable bridge request id and a `RESERVED` bridge reservation; a fresh runtime on the same PG recovers the abandoned job, requeues the same attempt, resumes, seals the turn as `orphaned_running_turn`, keeps `NEEDS_REVIEW`, commits the reservation once and keeps provider sends at 1; input/authorized/evidence hashes, prompt template id/version and requested model are exactly preserved; (b) *proven no-send*: the worker dies after the `RUNNING` turn + pre-submission ModelRun commit but before the submission claim; durable `NOT_SUBMITTED`, attempt 0, no bridge request id, provider sends 0; recovery preserves that proof, seals to `NEEDS_REVIEW`, sends 0 and invents no retry. **The framework pre-submission-proof tool-invocation branch remains NOT_EXERCISED.** M05-D terminal redelivery remains PASS (one committed/consumed turn, stable `NEEDS_REVIEW`, state/audit/model/hash/chronology unchanged, fake sends stay 1). |
| Canonical status/model/hash survive a new service instance on the same disposable PG | identical SQL/version/SHA | **PASS (M05-B PG)** — rebuilt app/service and transport replay the uncertain invocation; snapshot hashes and audit unchanged, replay transport sends 0 |
| Stale context cursor | HTTP 409, no silent reread | **PASS (M05-D PG)** — `stale_cursor.status_code == 409` |
| Invalid legacy offset | HTTP 422 | **PASS (M05-D PG)** — mixed cursor/offset request returns 422 |
| Context SHA independent of full vs paged reads | equal canonical SHA | **PASS (M05-D PG)** — full and every page expose the same `context_sha256` |
| Finding versions | current in context, history distinct | **PASS (M05-D PG)** — current context holds only v2; history returns v1 and v2 |
| Evidence order | `(observed_at, UUID)` with NULLs last | **PASS (M05-D PG)** — non-collinear fixture (larger UUID, earlier timestamp) served first; NULLs-last retained in the existing repository test |
| V5 → V6 publication | V5 preserved, V6 augmented, reviewer/approver in lineage metadata, not rendered as names automatically | **PASS (M05-D PG, real Typst/Poppler)** — both documents real PDFs; lineage actors present in manifest, names absent from extracted text |
| Canonical chronology appears once in the real PDF | `pdftotext` count/order assertions | **PASS (M05-D PG + M05-C)** — each chronology marker appears once, in order, before and after replay |

## Egress proofs

| Check | Status |
|---|---|
| Source audit: no external client constructed in fake E2E paths | **PASS (M05-A source audit + M05-D runtime)** — renderer imports only `advisor_wire`; D patches socket connect/connect_ex/sendto/sendmsg and DNS for the whole journey |
| Runtime: fake transport is the only submit port; counters asserted | **PASS (M05-B/C/D)** — every refusal asserts `transport.calls == []`; the allowed path asserts exactly one fake send |
| Runtime: no `httpx`/socket external call during E2E | **PASS (M05-D)** — the guard actively rejects a UDP probe to `203.0.113.9:9`, HTTP/DNS non-loopback names, and allows only loopback PostgreSQL/local tools |
| No dotenv/secret read by pure preparation | **PASS (M05-A)**: `test_prepare_accepts_pure_mapping_and_does_not_open_files_start_processes_or_connect` monkeypatches file/socket/subprocess/`get_settings` I/O to raise and still succeeds; the renderer module imports only `advisor_wire`. |
| No external client constructed by the renderer | **PASS (M05-A source audit)**: `advisor_prompt.py` imports only `collections.abc`/`typing`/`advisor_wire`; no httpx/socket/settings. |

## Closure-pass proofs (2026-10-25, code SHA `4111acf3`)

| Proof | Status |
|---|---|
| P10 absence in a no-sample investigation | **PASS** — the real `ProposalConversationService.propose()` raises the typed first-precondition failure `The investigation has no goodware baseline binding`; durable state has no subject Samples and no baseline binding; zero adapter calls, no P10 turn, no P09 call. |
| Real P10 with satisfied preconditions | **PASS** — persisted inert Sample + real imported/bound goodware index; real P10 snapshot; one local fake model send; a real P09 invariant; replay reuses the same turn with one send. |
| In-flight ambiguous recovery | **PASS** — see the replay row above. |
| In-flight proven no-send | **PASS** — see the replay row above. |
| No application code changed | **PASS** — `git diff --name-status a30a7c22..4111acf3` lists only three integration test files. |
| No live egress in the new tests | **PASS** — only local adapter fakes; no network client is constructed, and the provider fake asserts its send count. |
| Framework pre-submission-proof tool-invocation branch | **NOT_EXERCISED** — the in-flight oracle proves the model-run submission boundary; the tool-invocation proof verifier is not triggered. This is recorded, not claimed. |

## S3 / Advisor cutover

- `S3_BLOCKED_DESIGN`: unchanged. Admission, reconciliation, in-process lock and
  `adapter.execute` still do not form a durable physical authority. Phase 3
  fake oracles characterize risks only.
- `ADVISOR_CUTOVER_BLOCKED_BY_S3`: unchanged. Production submission remains v3
  authoritative; v4 is parse/reparse offline only; no dual send, no shadow
  parse, no v3 removal.
