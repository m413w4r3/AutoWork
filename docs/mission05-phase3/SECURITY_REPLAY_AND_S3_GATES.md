# Mission 05 — security, replay and S3 gates

Status: **OPEN** — populated as lots are integrated. Any row not backed by an
executed test is `NOT_DEMONSTRATED`, never PASS.

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
| `TLP:RED` envelope | disclosure refusal, no prepared dispatch | fake transport counter 0 | NOT_DEMONSTRATED |
| `do_not_submit=true` | disclosure refusal | fake counter 0 | NOT_DEMONSTRATED |
| `external_llm_allowed=false` | disclosure refusal | fake counter 0 | NOT_DEMONSTRATED |
| sample/source/owner restriction | disclosure refusal naming association IDs/hashes, never restriction text | fake counter 0 | NOT_DEMONSTRATED |
| AutoWork operational credential in payload | redaction or `secret_field_remaining` / credential refusal | fake counter 0 | NOT_DEMONSTRATED |
| signed URL / revoked authorization | credential/signed-URL refusal | fake counter 0 | NOT_DEMONSTRATED |
| canonical evidence absent / completeness not READY | `evidence_preflight_*` refusal | fake counter 0 | NOT_DEMONSTRATED |
| ref identity conflict (same kind/id, two SHAs) | `ref_reference_conflict` | fake counter 0 | NOT_DEMONSTRATED |
| evidence reference absent or from another investigation | `advisor_input_invalid` / `advisor_input_rejected`, `confirmed_non_execution=true`, `provider_usage.not_submitted` | fake counter 0 | **PASS (M05-F, unit + PG)** — RED `UnboundLocalError` at base line 1006, GREEN typed refusal at candidate `a1b356c6` / integrated `12671c6a` |

## Replay / restart invariants

| Invariant | Expected | Status |
|---|---|---|
| Restart with `RUNNING` or uncertain external outcome | reservation and diagnostics preserved; no second call on the same operation key; `NEEDS_REVIEW` unless canonical bounded non-submission proof | NOT_DEMONSTRATED |
| Canonical status/model/hash survive a new service instance on the same disposable PG | identical SQL/version/SHA | NOT_DEMONSTRATED |
| Stale context cursor | HTTP 409, no silent reread | NOT_DEMONSTRATED |
| Invalid legacy offset | HTTP 422 | NOT_DEMONSTRATED |
| Context SHA independent of full vs paged reads | equal canonical SHA | NOT_DEMONSTRATED |
| Finding versions | current in context, history distinct | NOT_DEMONSTRATED |
| Evidence order | `(observed_at, UUID)` with NULLs last | NOT_DEMONSTRATED |
| V5 → V6 publication | V5 preserved, V6 augmented, reviewer/approver in lineage metadata, not rendered as names automatically | NOT_DEMONSTRATED |
| Canonical chronology appears once in the real PDF | `pdftotext` count/order assertions | NOT_DEMONSTRATED |

## Egress proofs

| Check | Status |
|---|---|
| Source audit: no external client constructed in fake E2E paths | NOT_DEMONSTRATED |
| Runtime: fake transport is the only submit port; counters asserted | NOT_DEMONSTRATED |
| Runtime: no `httpx`/socket external call during E2E | NOT_DEMONSTRATED |
| No dotenv/secret read by pure preparation | **PASS (M05-A)**: `test_prepare_accepts_pure_mapping_and_does_not_open_files_start_processes_or_connect` monkeypatches file/socket/subprocess/`get_settings` I/O to raise and still succeeds; the renderer module imports only `advisor_wire`. |
| No external client constructed by the renderer | **PASS (M05-A source audit)**: `advisor_prompt.py` imports only `collections.abc`/`typing`/`advisor_wire`; no httpx/socket/settings. |

## S3 / Advisor cutover

- `S3_BLOCKED_DESIGN`: unchanged. Admission, reconciliation, in-process lock and
  `adapter.execute` still do not form a durable physical authority. Phase 3
  fake oracles characterize risks only.
- `ADVISOR_CUTOVER_BLOCKED_BY_S3`: unchanged. Production submission remains v3
  authoritative; v4 is parse/reparse offline only; no dual send, no shadow
  parse, no v3 removal.
