# Independent Bridge evaluation — Mission 07

**Scope:** investigation `a8845772-5025-4702-8850-2b6be776855b`; advisor consultations by CTI-ANALYST-6/7/8 and the orchestrator smokes named below. **Repository snapshot:** `ae26c39f1a2059ab61ab1e946f5c43999659359c`. Read-only review; no POST, live provider request, test, or evidence mutation was made. This report is an audit artifact, not CTI evidence.

## Executive findings

- The journal has **19** in-scope calls: 8 orchestrator smokes, 3 CTI-ANALYST-6, **0 CTI-ANALYST-7**, and 8 CTI-ANALYST-8. The canonical `/operator-status` GET at v158 agrees on all 19 invocation IDs, statuses, and final `response_refs`. It reports no active jobs, ambiguous calls, or invocations requiring reconciliation. G2 was approved, TLP CLEAR, and `external_llm_allowed=true`.
- **Nine calls consumed one Bridge turn; ten stopped before a Bridge turn.** Seven turn-consuming calls returned usable guidance; two consumed a turn without a usable review (`c6-104` and `c8-126`). The best new analytical discriminator was `c8-077`’s static call/data-flow test for a chain from primary-channel failure/HTTP 401 through GitHub Issues lookup, comment extraction and decryption. The analyst accepted the test criterion, then deferred it when available method-level tooling could not expose callers.
- Advice stayed technically bounded. It repeatedly separated exact archive membership from deployment/execution, generic HTTP/AES capability from the reported GitHub fallback, the source-reported ETW setting from verified configuration/runtime effect, and source attribution from independent attribution. The analyst marked advice untrusted and did not promote it to evidence or upgrade qualified findings. No disproportionate operation was recommended or performed.
- Review quality improved after wire-format corrections. `c8-129` and `c8-153` returned usable U1–U5 reviews; the final v6 review detected no new overclaim. `c8-136` returned five usable reviews but still marked all five units unprocessed and the whole result partial. `c6-104` returned no usable review after consuming a turn; `c8-126` returned a four-byte `null` result after consuming a turn.
- At least four confirmed zero-turn outcomes still had one Bridge turn reserved at v158: `orch-sm-bridge-plan2-1793193592`, `orch-sm-rel-test-1793195853`, `c8-134`, and `c8-151`. Global budget was 10 consumed / 18 reserved / 172 remaining. This leaves a reservation reconciliation defect or unresolved reservation state despite `confirmed_non_execution=true` and no reconciliation item.
- Request-write-to-outbox completion was 450–683 s for the nine Bridge-turn calls (median 520 s); two pre-submission UI failures took 495 s and 600 s. There is **no post-21:42 UTC advisor call** in the journal: latest is `c8-153`, completed 21:37:55 UTC. The 21:43 status read is not a consultation. Thus this archive cannot verify whether calls after the stated fix became faster. The supplied 56–114 s `model_runs.duration_ms` values were not present in the M07 journal/outbox; `/operator-status` does not expose model-run timestamps.

## Call inventory and latency

Elapsed time is processed-input file mtime (inbox write time preserved by rename) to outbox mtime. Relay code confirms sequential synchronous handling and 1.5 s polling (`.autowork-runs/m07/relay.py:28-71`). `Partial` alone is not a usability verdict; the result notes distinguish it.

| Call ID | Owner | Result / usable? | Bridge turns | Write → done |
|---|---|---|---:|---:|
| `orch-sm-bridge-plan-1793193566` | Orchestrator | invalid input; no Bridge output | 0 | 5 s |
| `orch-sm-bridge-plan2-1793193592` | Orchestrator | technical preflight block; no Bridge output | 0 | 272 s |
| `orch-sm-bridge-plan3-1793193882` | Orchestrator | partial, usable structured pivot; summary says names/VT relation do not establish member identity | 1 | 516 s |
| `orch-sm-rel-test-1793195853` | Orchestrator | technical preflight block; no Bridge output | 0 | 336 s |
| `orch-sm-rel2-1793202497` | Orchestrator | technical preflight block; no Bridge output | 0 | 342 s |
| `c6-084-bridge-post-analysis` | Analyst 6 | partial, usable structured pivot; summary/advisory text missing | 1 | 520 s |
| `c6-094-bridge-review-opinion-v2` | Analyst 6 | invalid review target; confirmed pre-transport non-execution | 0 | 145 s |
| `c6-104-bridge-review-opinion-v2-corrected` | Analyst 6 | partial, **unusable**; U1–U4 unprocessed after wire recovery failures | 1 | 518 s |
| `orch-sm-rawbytes-1793210846` | Orchestrator | explicit sample-disclosure restriction; correctly blocked | 0 | 560 s |
| `orch-sm-rawbytes2-1793215594` | Orchestrator | partial, usable structured hash-comparison test; summary missing | 1 | 450 s |
| `c8-073` | Analyst 8 | invalid input; confirmed pre-transport non-execution | 0 | 4 s |
| `c8-077` | Analyst 8 | partial, usable pivot advice | 1 | 504 s |
| `orch-replay73-136` | Orchestrator | invalid evidence ref; confirmed pre-transport non-execution | 0 | 4 s |
| `c8-126` | Analyst 8 | partial, **unusable**; all U1–U5 unprocessed; Bridge response was 4 bytes (`null`) | 1 | 683 s |
| `c8-129` | Analyst 8 | succeeded; U1–U5 usable | 1 | 539 s |
| `c8-134` | Analyst 8 | `conversation_unavailable` before submission; no turn | 0 | 495 s |
| `c8-136` | Analyst 8 | partial, five usable reviews but inconsistent unprocessed-unit metadata | 1 | 575 s |
| `c8-151` | Analyst 8 | `conversation_unavailable` before submission; no turn | 0 | 600 s |
| `c8-153` | Analyst 8 | succeeded; U1–U5 usable, no parser warnings | 1 | 601 s |

The nine consumed-turn requests together prepared **1,792,693 prompt bytes** and returned **30,800 Bridge response bytes**. Exact per-call byte accounting is below. Provider token/$ usage is unknown: pivot outputs report `provider_usage.status=usage_unavailable`, and the review outputs have no provider usage measurement. Reservation `context_tokens` are estimates, not measured model tokens.

| Call ID | Prepared prompt bytes | Bridge response bytes |
|---|---:|---:|
| `orch-sm-bridge-plan3-1793193882` | 279,168 | 5,022 |
| `c6-084-bridge-post-analysis` | 406,580 | 4,721 |
| `c6-104-bridge-review-opinion-v2-corrected` | 178,514 | 6,988 |
| `orch-sm-rawbytes2-1793215594` | 98,313 | 3,268 |
| `c8-077` | 174,742 | 4,942 |
| `c8-126` | 164,126 | 4 (`null`) |
| `c8-129` | 163,855 | 1,859 |
| `c8-136` | 163,626 | 1,612 |
| `c8-153` | 163,769 | 2,384 |

## Advice, dossier quality, and analyst decisions

### Pivot consultations

- `c6-084` asked Bridge to challenge bounded static analysis. Preflight was complete (42/42 required refs sent, 0 missing; 404,670-byte dossier). Despite the response’s missing summary/advisory text, its structured experiment clearly proposed comparing SHA-256 of the acquired ZIP members `Resources/PsProxy.dll` and `Resources/RuntimeBroker.dll` to the standalone candidates. This was technically sound and falsifiable, but it repeated the earlier M07 Bridge plan `a057-bridge-plan-reprise` (outside this scope), which had already suggested the same identity check. Analyst 6 **ACCEPTED** the criterion but **DEFERRED** execution until a sanctioned archive-hash tool was available; no evidence came from Bridge.
- Orchestrator `orch-sm-bridge-plan3-1793193882` sent 28/28 required refs (277,655-byte dossier) and received a usable but partial plan. It correctly treated VT `execution_parents` as correlation and ZIP member names as insufficient for binary identity. The requested “two or three checks” was only partly met: `discriminating_experiment` and `recommended_next_tools` were empty, while the hash test appeared as a discarded lead. `orch-sm-rawbytes2-1793215594` repeated the same exact-member hash test (98,313-byte prompt; 3,268-byte response, but no summary). It added little new analysis; Analyst 7’s later reasoning and Analyst 8’s `archive_extract_hash` result show the test was then executable. Exact equality established inclusion of the two analyzed DLLs in that ZIP, not execution/deployment or actor control; H1 remained qualified.
- `c8-077` had a focused, well-formed question about whether evidence changed H1/H2/H3 and requested one falsifiable static pivot. Its dossier sent all 50 required refs (172,158 bytes; 0 missing). Bridge correctly acknowledged the already established ZIP membership and static capabilities, then proposed checking a specific control/data-flow chain joining main-channel failure or HTTP 401, GitHub Issues search, encrypted comment extraction, and decryption. Analyst 8 **ACCEPTED** the exact-membership and static/runtime distinctions, **ADAPTED** “H1 reinforced” to the narrower archive-to-sample component relation, and **DEFERRED** the call-chain test when tools exposed method bodies but not callers. Subsequent bounded AES/helper reads did not prove the chain; H3 stayed qualified and source-attributed.

### Opinion reviews

- `c6-094` was versioned to opinion v2 but failed before submission because review-unit targets were invalid (`editorial_unit_target_invalid`); source trace identifies the required null unit anchor (`backend/src/cti_app/application/investigation_advisor_tools.py:4799-4830`). The corrected `c6-104` consumed one turn but the response parser recovered no header, prose, type, status, or review unit; U1–U4 remained unprocessed. Analyst 7 **REJECTED** it as an opinion review. This is a wasted turn and a prompt/parser contract failure.
- `c8-126` was properly bound to opinion v4 and its SHA and named all five blocks, but the prompt did not require the parser’s wire form. Bridge returned four-byte `null`; parser warnings were `wire_header_missing`, `wire_prose_ignored`, `type_missing`, `status_missing`, and `review_unit_missing`. It consumed a turn and yielded no usable unit. `c8-129` corrected the instruction to require `advisor-wire.v1` records and returned usable U1–U5. The remarks caught overclaim and citation-fit issues: `execution_parents` is not execution proof; rename “Chaîne d’infection” to “Capacités statiques”; separate reported ETW/GitHub claims from observed code; distinguish HTTP 401 from 403; qualify hash-only config, missing traces, and attribution. Analyst 8 **ACCEPTED/ADAPTED** these changes in v5. The successful response had empty per-unit `basis_refs`, so the analyst still had to validate each remark against the dossier.
- `c8-134` (v5, completed about 21:04 UTC) and `c8-151` (v6) are confirmed pre-submission `conversation_unavailable` failures. The mission log records the 21:04 dedicated-window creation failure; neither consumed a Bridge turn. The changed retries `c8-136` and `c8-153` were version-bound and explicitly required wire records. `c8-136` returned five usable remarks, refining the source-only `enabled="false"` claim, the 401/403 distinction, the lack of a demonstrated AES-to-GitHub chain, and the taxonomy title. But it was globally partial (`reference_list_partial`) while listing all U1–U5 as unprocessed despite returning usable review records for each. Analyst 8 **ACCEPTED/ADAPTED** the remarks.
- `c8-153` is the final v6 review with exact opinion SHA and all five units. It found no remaining overclaim: membership is distinct from execution and attribution; capabilities are static; config content/runtime ETW effect remain unverified; GitHub fallback remains source-attributed; 401 and 403 are separate; absence of traces is not proof of absence. Analyst 8 **ACCEPTED** the review and made no further edit. These reviews changed wording and confidence boundaries, not evidence state. No Bridge output was mistakenly used as evidence.

### Refusals and completeness defects

- `orch-sm-bridge-plan2-1793193592`, `orch-sm-rel-test-1793195853`, and `orch-sm-rel2-1793202497` were blocked in evidence preflight with the same two failures for one required observation: canonical blob/read capability unavailable and proof metadata unverifiable. The first two remain `needs_review`; the third is `failed`. Each consumed zero Bridge turns. The relay smokes exposed a repeatable missing-ref condition, not a Bridge answer. The `rel-test`/`rel2` purpose deliberately cited restricted raw bytes; `orch-sm-rawbytes-1793210846` was also correctly policy-blocked because four sample associations and 39 observations were explicitly restricted. This is governance working as intended, not a `do_not_submit` veto.
- `c8-073` used envelope `schema_version="advisor-consult.v1"`; the analyst log attributes its pre-transport rejection to that value (successful calls use `"1"`). The orchestrator replay `orch-replay73-136` used version `"1"` but still failed `_load_evidence` because an evidence ref was absent or belonged to another case. Both were confirmed non-execution with zero turns. No third retry was made; `c8-077` used a changed four-ref dossier.
- The two UI failures and the parser failures are separate: `conversation_unavailable` occurred before submission; parser failures occurred after a Bridge turn. Keep those statuses distinct. Output-level `confirmed_non_execution=true` on `c8-134`/`c8-151` conflicts with the generic outer error text “returned without a confirmed outcome.” Their canonical reservations remain `reserved`; the same is true for the two technical-blocked `needs_review` smokes. By contrast, other failed preflight calls were released. This inconsistency is the most concrete remaining state/accounting defect.

## Timing and transport interpretation

- The M07 relay is a single loop over sorted inbox files, runs each backend request synchronously, and never retries (`relay.py:28-71`). Thus a GET queued behind a POST waits for that POST; this audit did not use `pgrep`. Our 19 observation GETs completed in roughly 3–9 s each after the last consultation, but that does not bound prior POST queue wait.
- The provided runtime measurements place model-run start 7–9.5 minutes after request arrival, with only 56–114 s in `model_runs.duration_ms`, while relay completion took about 495–683 s. That is consistent with the file-timestamp durations above and with the reported backend CPU saturation / repeated `SELECT ... FROM blobs`. The fix is present at this snapshot: `_observation_references_sample` now memoizes parsed payloads and per-(observation, sample) answers (`backend/src/cti_app/application/investigation_advisor_tools.py:3811-3830`); its caller scans observations per associated sample (`:3107-3111`). There is no post-21:42 consultation to test runtime improvement.
- Other measured/structural delay sources: relay queueing behind earlier POSTs; backend pacer of 20 s plus up to 30% jitter; and UI window creation failures (`c8-134`, `c8-151`) which each spent 8–10 minutes before confirmed pre-submission failure. Pacing is small beside the observed multi-minute wait. The zero-turn smokes also took 272–560 s in three preflight cases, showing delay was not limited to model generation.
- The allowed `/operator-status` snapshot exposes invocation refs and budget state, not `model_runs.started_at`/`duration_ms`. The M07 archive has no model-run output file, so the 56–114 s figure is recorded as the supplied measurement, not independently verified here. The source audit projection does expose model-run start and `duration_ms`, joined through a Bridge conversation-turn ID (`backend/src/cti_app/application/investigation_audit_export.py:1105-1126,1555-1587`); the DB row also stores `finished_at` (`backend/src/cti_app/infrastructure/database/models/model_execution.py:94-121`). The permitted observation/status reads do not provide that projection.

## Canonical observation verification

The v158 status invocation list maps each call above to its final response observation. A GET header for all 19 observations returned HTTP 200 and the SHA below; only offset 0/16,384 chars was retrieved for each large record (and the tail page was additionally read for `c8-153`). Full payload pages were not downloaded. This verifies the canonical IDs/SHA and invocation linkage; substantive advice assessment also uses the archived relay outboxes and analyst log. `/operator-status` itself is a checkpoint, not a model-run timing record. The GET route and SHA validation are implemented at `backend/src/cti_app/api/investigation.py:1591-1635`.

Audit relay note: the first attempt to fetch `c8-153` used a malformed local request file (`ev1-2`, literal `\n`) and was rejected by the relay before API dispatch. It created `.autowork-runs/m07/processed/ev1-2.json.err`; the valid request `ev1-3` then returned HTTP 200. No operator call or investigation mutation resulted from `ev1-2`.

| Call ID | Canonical output observation | Observation SHA-256 |
|---|---|---|
| `orch-sm-bridge-plan-1793193566` | `f34e49eb-f929-518f-b75f-77005c472492` | `f91f90d2fa6d87fe8203339c672c77ff876222908f2322de0ebc05fa3f1e3b13` |
| `orch-sm-bridge-plan2-1793193592` | `29332ad5-3ff1-592d-8246-213688011cec` | `3600942263595cacee395b65e4899bc2460c3adcf474edb01db45c7fedf239c8` |
| `orch-sm-bridge-plan3-1793193882` | `aafebd78-0ea2-57ba-aa31-43bb71f38b96` | `e36f5948075f7aeda576c4de660f392a12220bc0b4abc8f9c50d9eb3bb6506cd` |
| `orch-sm-rel-test-1793195853` | `e965725d-bf4c-50f0-aa94-482b60ddb37e` | `3bdcfecb3ca76ebc90c46bd869d9fcaf636ee22559057035f0819cca07081ebe` |
| `orch-sm-rel2-1793202497` | `b046c607-be2c-5785-9e52-69dd343b034b` | `d2de386f77507083806e65984264bbd36fdc44d43a3036e5f30329a03a8b5a98` |
| `c6-084-bridge-post-analysis` | `1ff05731-c78d-54f7-9a69-2dd180d1a803` | `5d55061a9c29fe763bd33d77f40b83401ec58a7db7b4d7bf446b12aac9f43576` |
| `c6-094-bridge-review-opinion-v2` | `2ff5a529-4431-510d-9f42-7d0abe8cbac6` | `91cce9ac7cef5ac39918e18d8d2665f193b3dc0dd8daa0b5fd529c80473cb560` |
| `c6-104-bridge-review-opinion-v2-corrected` | `449ed0d4-6388-5d52-85df-5976b3142d8c` | `edb9b9af010c72235636be5417d979c58d5830b08ea5df34ea7b4bb9023d4edc` |
| `orch-sm-rawbytes-1793210846` | `61fba4cc-b3a7-567e-877f-c0b45f6f8ec9` | `e127e47f6ef7773c9b8c5b4b4d819179a60d5edd10984b102f2b912a0ced48cf` |
| `orch-sm-rawbytes2-1793215594` | `e48c5914-5b63-5d4f-83e6-eeb4d487bb83` | `174c5756f51c81ef2f7529b08c83acab82cbd1bebec22f4f68ce211a3f2ee0d1` |
| `c8-073` | `37309d2c-ca00-54ea-938b-ad2cab721740` | `a46002a5034c449b72a0cae46964d4764c51e2a381f7e6858fe01d581e5db6db` |
| `c8-077` | `a535e230-70fd-50d3-9c16-7450d1d5e047` | `85509d3b55f319003c09b3babdbfd0f565985ec67e8f9fb28e330d6a46868d8d` |
| `orch-replay73-136` | `ef769060-563c-55f0-a5f9-8c37f1e1ddbc` | `b3447599b6f833848edfb71bffae05866df21169c9f7b7663774ac1ab37622e0` |
| `c8-126` | `b0bf0bd2-27ed-52e4-a6f7-f8b937af01ce` | `997b81724414252edbf028d8447e5d0ec1bc9124df2ad5ec1a95b0297084190f` |
| `c8-129` | `f75ab4e9-1a82-5547-a8fe-692c5897f65e` | `b5f1d8c2be13c7334ef1e1cdf04103bf64bf7d143e6eeb09d8e6cdf001948f00` |
| `c8-134` | `b8e7d772-d4df-5f71-94f4-d430172ac4d9` | `1733ad18b31e00925ded0f39ce4130c0dc3bb8aa4440b07faef87caf534f6891` |
| `c8-136` | `f5c83bf0-df67-57c8-a4d0-de896573af22` | `9a2fa7c3d630e1421ed0b3105a0e03c3c4317c0d2e7f33aa66b6709dcf3373a4` |
| `c8-151` | `4940c38a-e953-5fdc-b188-5c9aa4c1b40b` | `603895b0e1cbaa00675ae33d28afc9b4390496e0707ff6074dfea013dab919a5` |
| `c8-153` | `e0e85a12-dd49-51b4-b018-5785afd643c5` | `f6fa86fa3bdc4890f4ce86059c773db4c82e6061008ae668c51ad7182d028075` |

## Next permissible check

To establish whether the memoized blob-read fix improved latency, capture a new authorized consultation’s request-write, model-run `started_at`/`duration_ms`, and relay completion, then compare the same measurements. Before additional advisor use, reconcile the four held one-turn reservations against their confirmed pre-submission/preflight failures.
