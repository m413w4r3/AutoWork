# Mission 07 checkpoint-1 audit — read-only observer

Audit date: 2026-10-28. Investigation: `a8845772-5025-4702-8850-2b6be776855b`; reported run `27478dcc-69d6-4593-89fb-7bd45e9c3537`; reported subject `d22822d4-27ce-47d2-9cc5-f84cf312986a`.

## Scope, evidence boundary, and blockers

- Reviewed only the specified journal, analyst log, checkpoint, handoff, Phase C contract, repo instructions, and current source locator output. No AutoWork API, VirusTotal, or Bridge calls were made; no provider or test was called; no secrets/runtime directory was read; no finding, investigation, or artifact was changed. The only write is this report.
- **Canonical verification blocked.** PostgreSQL was not listening on `127.0.0.1:5432` or `:55432`; Docker daemon access was denied. I did not attempt API fallback. Therefore every investigation status, budget, finding, observation, refusal reason, and artifact reference below is a trace-reported fact, not an independently re-read canonical DB fact. Treat canonical evidence state as **UNKNOWN**, not PASS. Observation UUIDs/SHA values are reproduced as reported only.
- Checkout discrepancy: this clean local checkout is `main` at `2ea40d4f006c474d9030d835b0cee5b3c02c4789` (`origin/main` same); the handoff says `HEAD`/`origin/main` were `4a18e688`. Deployment/code parity is therefore unknown.
- Traces at review time: journal SHA-256 `92e13978bf7418df8624dd29a96b2fc9d394a334ae6e8c0b079de918bbfa8aea`; analyst log `0de22b3bf02f1b291625b0f7cce024f7df8c1b6437026dc01e8e94c0f9b8a9cb`; checkpoint `e6deb507f8a5fe29444066e941b73903592cedb5230e74572fd71e4a6ce1624d`; handoff `786a04e8280cc170e617f5e918d5afca0cb03ebf444bdcd88f850cd979abedf3`; Phase C contract `08c07d32ffad3806304fb9ebd05d273f9355ab5c42e5695efec955dce4c2d765`.

## Blocking issues first

1. **Two external VT operations remain unresolved and must not be replayed.** `9aaec14d-fe80-4987-976c-b00187b8a891` (`vt.file_report`, hash `6e7d…`) and `36114580-ccbb-47a6-b170-3e9fa675e1d5` (`vt.relationships`, `bundled_files`, same hash) are reported `needs_review` / `external_effect_unconfirmed`, with no archived output. A retry could duplicate a send if the first effect happened. Two later attempts for the first operation (`a077`, `c016`) were rejected with HTTP 409 and the original invocation ID; they created no new invocation but did not resolve the old effect. Handoff says a human must reconcile `9aa…`; independently unverified here.
2. **Bridge provided no advice.** All 13 `advisor.consult` calls are reported as pre-submit refusals; each has zero Bridge turns and no `bridge_invocation_id`. The final `e015` passed dossier evidence preflight, then stopped before transport with `external_effect_authority_required` / `authority_context_missing`. This is a blocker in the authorized production path; do not bypass it or replay `e015`.
3. **Reservation leakage is reported, not independently confirmed here.** At v69 the checkpoint/handoff report Bridge `0 consumed / 13 reserved`, VT `22 consumed / 13 reserved`, downloads `0`, tool-seconds `31 consumed / 47,040 reserved`, context `1,378,325 bytes / 344,586 tokens`. The handoff says SQL confirmed the 13 Bridge reservations remain `reserved` after pre-send refusals. This looks like budget reservation release defect, not provider cost: Bridge provider usage is unavailable/zero turns. The separately reported internal blob `b23aa970-ef0f-4a92-b698-b69f07704ff1` (353,028 bytes; SHA `b4b1777f56db5ebe33b2136e35d2bb62a543402933724a3c6156e16561bb0a85`) contains sample data rendered as Base64; it was reported not transmitted, read, or deleted. Leave it untouched.
4. **Final opinion is not auditable.** `128f991d-09c9-5cb5-905c-d0c044bfff00` v1 / SHA `6a7529373a213b5b263a6aa06a8304e4519bb9862cb64f23c3a4ef9ba410dcbd` was composed, but the body was not readable through the operator scope (403); three later revisions failed `citations_invalid`. It is not Bridge-reviewed, approved, published, or PDF-validated. It still predates the last H1 revision and may refer to a superseded finding.

## Decision audit

The fields below use the information recorded as available before the action. Where the analyst log gives only a post-action reconstruction, that limitation is explicit. Repeated tool calls are grouped only when they tested the same question; the invocation ledger below records every POST separately.

### D00 — Establish context and tool contracts; relay IDs `a001`–`a014`; GETs only

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** no current investigation status or question-specific source facts had been read.
- **ACTION_CHOSEN:** read status, summary, claims, samples, indicators, open questions, and tool catalog; paginate claims and tool schemas.
- **EXPECTED_INFORMATION_GAIN:** establish policy/budget/state, exact claim/sample mappings, and valid tool schemas before writes or external pivots.
- **OBSERVED_RESULT:** trace says v1/running, OSINT_OPEN active, TLP CLEAR, external consultation allowed, no prior invocation; synthesis `4d37c738-d78f-4087-b251-3c38a56e8e53` v1 / SHA `8eb9cbe0…`; 31 claims, 15 sample hashes, 59 indicators, 10 questions. Three catalogue requests failed because offset/cursor/CAS were used incorrectly; the correct working-state SHA then succeeded. No budget use.
- **RELEVANCE_ASSESSMENT:** useful discovery; three avoidable navigation errors, no analytic result or external effect.
- **ALTERNATIVE_IF_MATERIAL:** use the returned cursor and working-state SHA on the first retry.
- **SEVERITY:** Low.
- **CANONICAL_EVIDENCE_REFS:** synthesis/artifact above; page SHAs in analyst log L21–L54; all trace-only pending DB.

### D01 — Declare H1–H4; `a015`, `a017`, `a019`, `a021`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** claims mapped sample linkage, process-scoped ETW, GitHub fallback, and attribution limits; article text was explicitly untrusted input.
- **ACTION_CHOSEN:** define testable H1–H4 with bounds against treating generic labels, absent results, capability, or name similarity as confirmation.
- **EXPECTED_INFORMATION_GAIN:** turn source statements into discriminating checks and keep attribution separate from technical linkage.
- **OBSERVED_RESULT:** all four declarations succeeded. Later current findings were reported as H1 `f6f0a34e-8207-5e1c-9040-b17cb49b95d8`, H2 `2b02047e-949e-500f-97ce-dc52a2f41ac2`, H3 `dd157c98-921e-5194-b863-f00fc899ebc8`, H4 `746907e4-ccde-53a5-b073-0e189976758c`; all qualified at v69.
- **RELEVANCE_ASSESSMENT:** sound framing; no unsupported attribution inference in the declared criteria.
- **ALTERNATIVE_IF_MATERIAL:** none.
- **SEVERITY:** Low.
- **CANONICAL_EVIDENCE_REFS:** synthesis above; declaration observations `da03d6b3…`, `7a62ea7f…`, `63876e95…`, `a7c840df…` (IDs as reported, DB unverified).

### D02 — First VT pivot on archive hash; `a023` / `9aaec14d-fe80-4987-976c-b00187b8a891`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** synthesis mapped `6e7d9b33…1fd239` to the DubaiAirport ZIP; H1 asked whether a specific technical link existed.
- **ACTION_CHOSEN:** passive `vt.file_report`; no upload/rescan.
- **EXPECTED_INFORMATION_GAIN:** learn exact identity, detections, metadata, and specific links.
- **OBSERVED_RESULT:** `needs_review`, `adapter_output_archived=false`, `external_effect_unconfirmed`; observation `ade36e70-4609-51f8-b7d5-f38d3308ecaf` / SHA `e0e42f84…`. Log says refused before a new send, but the persisted outcome does not confirm non-execution.
- **RELEVANCE_ASSESSMENT:** no VT result; correct not to treat this as negative. External effect unresolved.
- **ALTERNATIVE_IF_MATERIAL:** stop and obtain supported reconciliation; do not replay.
- **SEVERITY:** High, because an unconfirmed external effect creates duplicate-send risk.
- **CANONICAL_EVIDENCE_REFS:** invocation/observation above; trace only.

### D03 — First Bridge preflight/input refusal; `a027` / `0a1f6297-9bfd-4241-b4ee-966d92cacabd`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** H1–H4 were open; VT was unresolved; policy was reported to permit a Bridge plan.
- **ACTION_CHOSEN:** request one local pivot plan for H2/H3 using synthesis and observation refs.
- **EXPECTED_INFORMATION_GAIN:** obtain a bounded next offline pivot and measurement.
- **OBSERVED_RESULT:** `invalid_input`, `advisor_input_rejected`, `pre_transport_validation`; `confirmed_non_execution=true`, provider `not_submitted`, zero turns, no Bridge ID. Observation `83386c9e…` / SHA `7e1b14f9…`.
- **RELEVANCE_ASSESSMENT:** procedural rejection, no analytic advice. Likely dossier/input validity issue, not a CTI finding.
- **ALTERNATIVE_IF_MATERIAL:** validate canonical observation UUIDs and consultation shape once; that was attempted.
- **SEVERITY:** Low.
- **CANONICAL_EVIDENCE_REFS:** observation above; trace only.

### D04 — Bridge secret preflight; `a030` / `41146d23-34bd-4a73-9d51-27471cb5847b`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** `a027` had confirmed no submission and invalid input; analyst reformulated with five present observation UUIDs.
- **ACTION_CHOSEN:** retry one `plan_investigation` with valid refs.
- **EXPECTED_INFORMATION_GAIN:** get a concrete H2/H3 plan.
- **OBSERVED_RESULT:** `refused`, `autowork_credential_or_signed_url_detected`, `secret_preflight`, two fields marked; confirmed non-execution, zero turns. Observation `8d51ae03…` / SHA `b572c6d1…`.
- **RELEVANCE_ASSESSMENT:** policy gate stopped transmission; log attributes the flagged content to an internal `operation_key` field, but the marked values are not exposed, so false-positive cause is trace-reported, not independently verified.
- **ALTERNATIVE_IF_MATERIAL:** omit the refusal observation and stop if detector repeats; next call did this.
- **SEVERITY:** Medium.
- **CANONICAL_EVIDENCE_REFS:** observation above; trace only.

### D05 — Repeated Bridge secret refusal; `a033` / `040b0ae8-dd10-4298-96ec-fe697127c946`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** `a030` had the same secret-preflight refusal; the analyst removed the VT refusal reference but the request assembler reportedly included the prior Bridge-refusal observation.
- **ACTION_CHOSEN:** one corrected, narrower dossier.
- **EXPECTED_INFORMATION_GAIN:** same plan, with flagged material excluded.
- **OBSERVED_RESULT:** same `autowork_credential_or_signed_url_detected` / `secret_preflight`, two fields, confirmed non-execution, zero turns.
- **RELEVANCE_ASSESSMENT:** no advice; repeating the same outcome justified stopping this branch. Likely detector/assembly problem, not permission to bypass the gate.
- **ALTERNATIVE_IF_MATERIAL:** stop and diagnose the internal field through an authorized code path; do not transmit or copy it.
- **SEVERITY:** Medium.
- **CANONICAL_EVIDENCE_REFS:** refusal observation cited at analyst log L149–L155 (`8d51ae03…`); exact separate observation for `a033` not identified in the supplied notes; trace-only.

### D06 — Local static attempt before association; `a041` / `f3cdba7b-e731-4c15-9f88-62f50912e25a`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** source mapped `f5b127…` to FlightManager.csproj, but the blob was not yet associated with this investigation (`sample_deltas=0`).
- **ACTION_CHOSEN:** bounded `strings` command for `GetFrameworkPaths`/MSBuild markers.
- **EXPECTED_INFORMATION_GAIN:** determine whether the exact file contained a specific hook.
- **OBSERVED_RESULT:** `policy_denied` / `tool_policy_denied`, reason `sample_not_associated`; no strings output. Observation `d584c57b…` / SHA `241e6b19…`.
- **RELEVANCE_ASSESSMENT:** appropriate association guard, not a negative analytic result. Later acquisition/analysis used the supported path.
- **ALTERNATIVE_IF_MATERIAL:** associate an eligible sample first; later `c022` did that.
- **SEVERITY:** Low.
- **CANONICAL_EVIDENCE_REFS:** denial observation above; trace only.

### D07 — Initial findings; `a045`, `a047`, `a049`, `a051`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** H1 access failures were not content observations; source passages bounded ETW, fallback capability, and attribution.
- **ACTION_CHOSEN:** propose H1 `inconclusive`; H2–H4 `qualified` with claim refs and explicit limitations.
- **EXPECTED_INFORMATION_GAIN:** preserve current interpretation without promoting missing or source-only claims to confirmed.
- **OBSERVED_RESULT:** all proposals succeeded. H1 referenced the two denial observations; H2–H4 used source-claim refs and did not have independent sample evidence.
- **RELEVANCE_ASSESSMENT:** epistemically cautious as logged; H2–H4 are source-only claims, not independent confirmation.
- **ALTERNATIVE_IF_MATERIAL:** retain qualified/source-attributed language; do not infer absent behavior.
- **SEVERITY:** Medium if later presented as independently observed; low in their recorded qualified state.
- **CANONICAL_EVIDENCE_REFS:** finding IDs above; H1 refs `ade36e70…` / `d584c57b…`; H2/H3/H4 creation observations `b5d25d17…`, `9ca05ea2…`, `9671512c…` (all trace-only).

### D08 — Bridge evidence-completeness loop; `a057`, `a062`, `a072`, `a074`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** all hypotheses/findings were qualified or inconclusive; no finding was refuted. Earlier Bridge tries had failed pre-submit; a new resumed plan sought advisory value before more pivots.
- **ACTION_CHOSEN:** progressively add explicit evidence/finding refs, retrieve original finding-observation bodies, then try retrieval observations.
- **EXPECTED_INFORMATION_GAIN:** get a plan while preserving the non-refuted state and avoiding unsupported contradiction claims.
- **OBSERVED_RESULT:** `a057` (`a1db79a4…`) had `required_canonical_evidence_not_in_payload`, 43/51 present, no explicit `evidence_refs`; 8 claims were reported as missing refuted findings. `a062` (`b091abe4…`) had 43/48 present and five missing observation bodies. `a072` (`dc0c3d0a…`) still said original bodies were not loaded into dossier. `a074` (`41dfa6b5…`) moved the missing-body requirement to the `evidence.get` retrieval observations themselves. All zero turns, no Bridge ID, confirmed non-execution.
- **RELEVANCE_ASSESSMENT:** first omissions were dossier-construction errors; the recursive requirement after retrieval is a likely preflight/data-contract defect. Four submissions yielded no advice; stopping after the repeated retrieval failure was appropriate.
- **ALTERNATIVE_IF_MATERIAL:** one supported contract clarification or product fix, then a single new attempt; do not create repeated retrieval artifacts to chase a self-referential gate.
- **SEVERITY:** Medium; no provider disclosure, but substantial wasted context/reservations.
- **CANONICAL_EVIDENCE_REFS:** `a057` refusal `e147bfd1…` / `77ac1724…`; `a062` `18a53b1e…` / `d2a52ea4…`; `a074` `6c631acc…` / `b77ef5b8…`; `a072` dossier SHA `4c5e3d59…`. Trace only.

### D09 — Resume same ZIP VT pivot; `a077` and `c016` (no new invocation IDs)

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** old `a023` remained `needs_review`; relay rejected same hash with reconciliation-required HTTP 409. A fix was reported, but canonical outcome still did not show an archived report.
- **ACTION_CHOSEN:** try the passive file report once after the reported fix, then later the same pivot was attempted again.
- **EXPECTED_INFORMATION_GAIN:** verify whether the reported reference fix enabled access to the exact hash.
- **OBSERVED_RESULT:** both attempts were blocked by `ambiguous_tool_invocation_requires_reconciliation`, pointing to `9aaec14d…`; no new invocation or VT result.
- **RELEVANCE_ASSESSMENT:** no analytic gain. The first retry followed a reported authorization to retry; the later repeat after the same guard added no information. Neither 409 proves the original effect did not happen.
- **ALTERNATIVE_IF_MATERIAL:** obtain supported human reconciliation of `9aa…`; do not choose a neighboring hash to bypass it.
- **SEVERITY:** High because the original external effect stays uncertain.
- **CANONICAL_EVIDENCE_REFS:** original observation `ade36e70…` / `e0e42f84…`; 409 relay IDs `a077`, `c016`; trace only.

### D10 — Passive VT component pivots; `b005`–`b066` (individual POSTs below)

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** current synthesis mapped documented hashes to components; H1 was technical association, not execution/attribution. Empty relations or generic labels were explicitly not treated as negative evidence.
- **ACTION_CHOSEN:** file reports, specific `bundled_files`/`execution_parents`/network relations and behavior reports, followed by scoped H1 revisions. Representative batches: FlightManager (`b005`, `b008`, `b013`); PsProxy (`b018`–`b027`); RuntimeBroker (`b029`–`b042`); Blackwood/parent archives (`b054`–`b067`).
- **EXPECTED_INFORMATION_GAIN:** establish exact hash-to-name mappings, parent/archive relations and any relevant observed behavior; refine H1/H4 without inferring execution or actor identity.
- **OBSERVED_RESULT:** trace reports 22 VT units consumed in total; empty results included dropped files/network/domains and some behavior; non-empty VT results were provider observations only. H1 moved from inconclusive → qualified → supported for a narrow technical association, then to current qualified after complete ZIP central-directory evidence showed no `FlightManager.csproj` member and member hashes were not matched. H4 remained qualified. `b007` had invalid `type/id/relationship` arguments; `b008` corrected them. `b067` lacked required revise fields and failed before mutation.
- **RELEVANCE_ASSESSMENT:** pivot ordering was generally question-driven. The H1 narrowing after ZIP inspection was evidentially responsive; empty relations do not disprove article claims. A few avoidable argument mistakes did not create external effects.
- **ALTERNATIVE_IF_MATERIAL:** keep unresolved relations as unknown; request complete member hashes/behavior evidence before strengthening archive linkage.
- **SEVERITY:** Medium for earlier transient H1 `supported` scope; current final state is reported qualified. Low for corrected argument errors.
- **CANONICAL_EVIDENCE_REFS:** VT report `d165de2f…`, PsProxy `6f843ae6…`, RuntimeBroker `aa4aa533…`, Blackwood `dba82ae8…`, archive refs `f208e795…` / `131540bb…`, and ZIP directory `a8b8de9b…` / `ceca5ee2…`; current H1 `f6f0a34e…`. All from log, DB unverified.

### D11 — Acquire/reuse and statically inspect samples; `c022`, `c023`, `c029`, `c030`, `c035`, `d033`, `d034`, `d038`, `d039`, `d047`, `d053`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** a bounded project-level question and an archive-manifest question remained; sample association was required; no sample execution was authorized or performed.
- **ACTION_CHOSEN:** reuse eligible quarantined samples; run static analysis/strings and read-only ZIP central-directory windows; revise H1 only to the measured scope.
- **EXPECTED_INFORMATION_GAIN:** inspect exact project contents and archive member names; compare the claim mapping without executing content.
- **OBSERVED_RESULT:** FlightManager static analysis was partial but showed the target, resource staging, and `Exec`; no automatic trigger was proven. PsProxy string heap had no returned user strings, not a refutation. ZIP analysis partial; full central directory listed 53 entries and no `FlightManager.csproj`; member hashes were not extracted/matched. H1 current at v64 became qualified. Downloads were reported reused; download budget remained 0.
- **RELEVANCE_ASSESSMENT:** useful bounded static observations, with tool limitations preserved. Good that no execution or automatic-trigger claim was made.
- **ALTERNATIVE_IF_MATERIAL:** compare member hashes and inspect included DubaiAirport project if an approved, bounded static path is available.
- **SEVERITY:** Medium for unresolved member identity; no unsafe execution observed.
- **CANONICAL_EVIDENCE_REFS:** project `b7a77bc8…` / `74af1208…`; ZIP acquisition `b2a7192e…`, association `7744da92…`; directory windows `a8b8de9b…`, `ceca5ee2…`; H1 `f6f0a34e…` / versions as reported.

### D12 — Compose internal opinion; `c040` / `84c513d5-dc3e-4eb3-9bcd-3c464cac3c38`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** H1 was reported supported for a scoped association; H2–H4 qualified; Bridge had not produced advice.
- **ACTION_CHOSEN:** compose an internal draft using four findings and selected source claims; no publication/PDF.
- **EXPECTED_INFORMATION_GAIN:** consolidate current analysis for later review.
- **OBSERVED_RESULT:** compose succeeded, opinion `128f991d…` v1 / SHA `6a752937…`; body unavailable through the scope (403). Later evidence changed H1 and no Bridge review occurred.
- **RELEVANCE_ASSESSMENT:** draft existence verified only by trace; its claims cannot be checked. It must not be treated as an approved or final conclusion.
- **ALTERNATIVE_IF_MATERIAL:** retrieve through an authorized read path, revise against current finding versions, then independent review.
- **SEVERITY:** High as a deliverable audit gap; not a publication event.
- **CANONICAL_EVIDENCE_REFS:** opinion ID/hash and findings in D01/D10; trace only.

### D13 — Later Bridge plan retries; `c013`, `d008`, `d014`, `d021`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** H1–H4/findings and VT/static results were available; no finding was refuted; neutral plan/contradiction search requested.
- **ACTION_CHOSEN:** submit a dossier with claim excerpts and observation refs; after refusals, remove claim labels and try once more after a reported guard fix.
- **EXPECTED_INFORMATION_GAIN:** independent critique and a recommended next pivot.
- **OBSERVED_RESULT:** `c013` (`8befcade…`): `insufficient_evidence`, `required_canonical_evidence_not_in_payload`, 61/71; ten passages were rejected with “No refuted finding is linked to requested claim passage.” `d008` (`233eff34…`): 61/70, nine equivalent missing-passage reasons. `d014` (`51701daa…`): after claim labels removed, one missing item said H1 was “cited as a contradiction target but no refuted finding was loaded.” `d021` (`b16c1170…`): same H1 selection persisted after the reported fix. All zero turns, no provider submission.
- **RELEVANCE_ASSESSMENT:** first payloads may have over-specified claim IDs/refs; later refusals on neutral planning and after a claimed fix indicate a likely software/preflight defect. `d021` was one instructed verification attempt, not an unjustified loop; no further retries were warranted.
- **ALTERNATIVE_IF_MATERIAL:** validate deployed guard against neutral-plan fixtures offline; resume only after fix and a fresh canonical state check.
- **SEVERITY:** High operational blocker; no CTI advice obtained.
- **CANONICAL_EVIDENCE_REFS:** `c013` `e241ab1b…` / `6ab1dd89…`; `d008` `238d5b44…` / `f166110c…`; `d014` `61176a63…` / `57953cc8…`; `d021` `8a64e16b…` / `0e036bda…`. Trace only.

### D14 — Ambiguous ZIP relation, reuse, manifest, and H1 revision; `d030`, `d033`, `d034`, `d038`, `d039`, `d047`, `d053`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** exact ZIP report remained unavailable; RuntimeBroker relation pointed to the ZIP; manifest presence could answer whether the mapped project was actually inside.
- **ACTION_CHOSEN:** query `bundled_files`, reuse/download the existing ZIP into the investigation, analyze statically, inspect two bounded central-directory windows, then narrow H1.
- **EXPECTED_INFORMATION_GAIN:** inspect membership and distinguish ZIP linkage from a separately mapped project.
- **OBSERVED_RESULT:** `d030` (`36114580…`) remained `needs_review` / `external_effect_unconfirmed`, no VT output; no retry. `d033` reused the 134,661-byte sample, `d034` partial; full central directory had 53 entries and no `FlightManager.csproj`. H1 revised to qualified, with member hashes still unknown.
- **RELEVANCE_ASSESSMENT:** useful local/associated evidence, but `d030` has unresolved external-effect risk. H1 narrowing is material and appropriately limited.
- **ALTERNATIVE_IF_MATERIAL:** reconcile `d030` before any retry; compare exact member hashes before linking samples.
- **SEVERITY:** High for `d030` duplicate risk; Medium for remaining analytical linkage gap.
- **CANONICAL_EVIDENCE_REFS:** ZIP observation `b2a7192e…`; d030 refusal `3c2c6b02…` / `88d9cbcd…`; directory refs above; current H1 `f6f0a34e…` (trace only).

### D15 — Opinion revisions; `d057`, `d061`, `d064`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** latest ZIP evidence and current findings differed from the v1 opinion; the analyst tried three progressively narrower citation payloads.
- **ACTION_CHOSEN:** revise the draft, then correct citations twice.
- **EXPECTED_INFORMATION_GAIN:** align the opinion with new findings and evidence.
- **OBSERVED_RESULT:** all three failed `citations_invalid`; no failing citation ID or violations were returned. Trace says opinion remained v1/hash above. Handoff suggests `fact` blocks cited only `qualified` findings; that is a hypothesis, not verified from the error payload.
- **RELEVANCE_ASSESSMENT:** repeated failures with opaque validation output are a tool-diagnostic defect/usability issue; after the third failure, stopping was reasonable.
- **ALTERNATIVE_IF_MATERIAL:** expose citation violations, inspect current schema, make one validated revision; no further blind mutation.
- **SEVERITY:** High for final opinion blocker; medium for diagnostic defect.
- **CANONICAL_EVIDENCE_REFS:** failed invocation IDs in ledger; error observations `73d77e1f…`, `26ec5692…`, `20fff455…`; trace only.

### D16 — Final Bridge evidence preflight; `e009` / `bc4acebb-2d66-4543-aeba-fc12fd64aa30`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** v67, active OSINT_OPEN/G2/TLP state was reported; findings qualified; a plan was requested before further analysis; no active job.
- **ACTION_CHOSEN:** submit passages, criteria, findings, VT/static observations and ZIP manifest; omit prior refusal observations, raw bytes, credentials, and superseded H1 target.
- **EXPECTED_INFORMATION_GAIN:** Bridge critique and a plan for unresolved H1–H4.
- **OBSERVED_RESULT:** 65/68 present, three blob observations lacked readable/verified canonical bodies or proof metadata; exact reasons: “Required canonical blob is unreadable or its read capability was not verified” and “Required canonical proof lacks verifiable metadata.” `required_canonical_evidence_not_in_payload`, evidence preflight/prevalidation; `bridge_invocation_id=null`, zero turns, no provider data transmitted. Observation `136da54f…` / SHA `fa8696c0…`.
- **RELEVANCE_ASSESSMENT:** data/read-capability problem, not an analytic contradiction; dropping the unreadable refs once was a material correction.
- **ALTERNATIVE_IF_MATERIAL:** keep only readable observations already in the dossier; this was attempted once.
- **SEVERITY:** High blocker, no external send.
- **CANONICAL_EVIDENCE_REFS:** observation above; missing refs `16ee954c…`, `46931ae8…`, `b2a7192e…`; trace only.

### D17 — Corrected final Bridge attempt; `e015` / `11db4592-7107-47e5-955d-e78c27ecd0e1`

- **INFORMATION_AVAILABLE_BEFORE_DECISION:** e009 identified three unreadable blobs; current findings remained qualified; preflight did not require a refuted finding.
- **ACTION_CHOSEN:** remove those refs/attachments and submit one corrected plan with 26 readable observations.
- **EXPECTED_INFORMATION_GAIN:** same Bridge review after satisfying evidence completeness.
- **OBSERVED_RESULT:** 65/65 present, three omitted; `preflight_status=READY`, then `failure_reason=external_effect_authority_required`, `authority_context_missing`, `pretransport_failure`, `submission_state=pre_submission`, `confirmed_non_execution=true`, no Bridge ID, 0 turns, 0 response bytes, provider usage unavailable, redacted-secret count 0. Observation `e384ca82…` / SHA `c5b659ab…`. No advice.
- **RELEVANCE_ASSESSMENT:** the corrected dossier passed evidence validation; production effect-authority wiring/policy context blocked transport. The gate must not be bypassed. Handoff calls the missing binding a likely wiring defect; that diagnosis was not independently verified here.
- **ALTERNATIVE_IF_MATERIAL:** authorized production fix, then a new consultation only after canonical state and invocation reconciliation; do not replay e015.
- **SEVERITY:** Critical mission blocker.
- **CANONICAL_EVIDENCE_REFS:** observation above; internal non-transmitted blob `b23aa970…` / `b4b1777f…`; trace only.

## All Bridge refusal reasons (13 calls)

All are trace-reported as zero turns and no Bridge invocation ID; reasons below are not provider responses. Early rows use `needs_review` as the AutoWork tool record status, not evidence that Bridge answered.

| Relay decision / tool invocation | Exact reported reason | Category at the time | Assessment / material next step |
|---|---|---|---|
| `a027` / `0a1f6297-9bfd-4241-b4ee-966d92cacabd` | `advisor_input_rejected`, `invalid_input`, `pre_transport_validation` | Bad/incomplete request | Correct the references once; done. |
| `a030` / `41146d23-34bd-4a73-9d51-27471cb5847b` | `autowork_credential_or_signed_url_detected`, `secret_preflight`, 2 fields | Policy refusal; likely detector false positive per log | Do not bypass; omit affected ref. |
| `a033` / `040b0ae8-dd10-4298-96ec-fe697127c946` | Same code/stage, 2 fields | Repeated policy refusal / likely assembler-detector issue | Stop this branch; done. |
| `a057` / `a1db79a4-e39d-40c4-8506-b7bdeb67190d` | `insufficient_evidence` / `required_canonical_evidence_not_in_payload`; 43/51; 8 passages demanded a refuted finding | Dossier incomplete plus preflight semantics | Add explicit refs once; done. |
| `a062` / `b091abe4-0612-409d-8dc4-7fca62a316dc` | `needs_retrieval`; 43/48; five observation bodies missing | Data/dossier body retrieval | Load bodies once; done. |
| `a072` / `dc0c3d0a-0c70-4cb1-a62a-6222eca6ac3a` | `needs_retrieval`; original H2/H3/H4 bodies still “not loaded into dossier” | Dossier assembly/tool contract | Add retrieval refs once; done. |
| `a074` / `41dfa6b5-3ef6-4bd6-a36e-ed3b4ac4414f` | `needs_retrieval`; now required bodies of the `evidence.get` observations themselves | Likely recursive software/preflight defect | Stop; done. |
| `c013` / `8befcade-0de7-42f7-97b3-f4cb5ae93ec9` | `insufficient_evidence`; 61/71; claim passages each “No refuted finding is linked to requested claim passage” | Likely software defect for neutral plan | No refutation should be fabricated. |
| `d008` / `233eff34-dd4a-4eb6-ba77-d6e15499e453` | `required_canonical_evidence_not_in_payload`; 61/70; 9 passages had same no-refuted-finding reason | Likely software/preflight defect; possibly over-specific payload | Remove claim IDs once; done. |
| `d014` / `51701daa-094c-4d6f-aa30-f44a517e2aec` | 61/62; H1 “is cited as a contradiction target but no refuted finding was loaded” | Likely software/preflight defect | One retry after reported fix; done. |
| `d021` / `b16c1170-f65e-45e8-9d55-a46b148254c1` | Same H1 contradiction-target reason after reported fix | Software defect persisted or deployed version mismatch | Stop; done. |
| `e009` / `bc4acebb-2d66-4543-aeba-fc12fd64aa30` | 65/68; `required_canonical_evidence_not_in_payload`; three blobs unreadable/unverified or lacking metadata | Data/read-capability problem | Drop those refs once; done. |
| `e015` / `11db4592-7107-47e5-955d-e78c27ecd0e1` | Evidence preflight ready; then `external_effect_authority_required`, `authority_context_missing`, pretransport | Effect-authority gate; likely wiring defect | Do not bypass/replay; fix via authorized path. |

## Analytical claims, evidence limits, and unverified conclusions

- The checkpoint reports current H1–H4 all `qualified`, none `confirmed`. H1 `f6f0a34e…` is qualified with 28 observation refs; H2–H4 are qualified/source-attributed and lack independent sample evidence. The intermediate H1 `supported` versions (`ce1d…`, `969fd…`) were superseded; do not reuse them as the current assessment. The last H1 revision narrowed the claim after the ZIP manifest showed no `FlightManager.csproj` member; no member hashes were matched.
- No source-only H2/H3/H4 statement should be rewritten as an independently observed operational fact. Empty VT relations, unavailable/partial tools, a hash label, and a generated refusal observation are not negative or positive evidence for broader claims.
- Opinion v1 body is not present in the supplied logs. Its exact sentences and evidence links cannot be audited. It was composed against H1 `969fd…` supported, before the later `f6f0…` qualified revision; the three attempted revisions failed. Treat the opinion as stale/unreviewed, not final evidence.
- The handoff’s suspected `citations_invalid` cause (fact block citing qualified findings) is plausible but not established by the returned error payload; the error had no violating citation ID.

## Repeated, useless, or blocked pivots; classification

- Tool-navigation/argument mistakes: `a008` offset pagination, `a009` missing CAS, `a010` context SHA used instead of working-state SHA; `b003` offset `0`; `b007` wrong relationship argument names; `b049` requested limit 50 above max 20; `b067` missing required finding-revision fields. These are analyst/procedure input errors, not CTI results; later corrected or stopped, low severity.
- Reconciliation repeats: `a077` and `c016` tried the same `6e7d…` VT report under new relay IDs and were blocked by the same unresolved `9aa…`; no new invocation or result. No external duplicate is evidenced by the 409s, but original effect is still uncertain.
- Bridge loops: 13 consults in total, each no advice; four attempts (`a057/a062/a072/a074`) cycled through completeness/body gates; four (`c013/d008/d014/d021`) cycled through claim-refutation preflight; `e009/e015` were the final pair with data then authority blockers. The correction attempts had material new information at each step, but continuing past the repeated self-referential gate or repeated same refusal would have been useless. No Bridge response exists to accept/adapt/reject.
- `a041` `sample_not_associated` was a policy guard and appropriate denial, not a software malfunction. Later reuse/association resolved access through the intended path.
- `sample.analyze` `partial` and unavailable CAPA/FLOSS/SMDA are capability/data-format limits, not proof of absence. `d030` and `a023` remain external-effect uncertainty, not policy refusal.

## Reservations, costs, transmission, and duplicate-send risk

- Reported final checkpoint budget: VT `22 consumed / 13 reserved / 9,965 remaining`; Bridge `0 / 13 / 187`; downloads `0 / 1,000`; tool-seconds `31 / 47,040 / 39,329`; Shodan `0`. Context reservations: `1,378,325` bytes and `344,586` tokens. Provider Bridge usage unavailable; zero Bridge turns reported. Downloads were reportedly content-reused, so no new download unit was consumed.
- The 13 advisor reservations align with 13 advisor POSTs; reservation status remaining `reserved` after pre-submit refusals is a reported accounting defect. No provider spend for those Bridge attempts is reported. Do not call reserved budget “consumed cost.”
- The e015 dossier renderer created the local Base64 sample blob listed above despite the visible dossier being described as 26 readable observations and short claim excerpts. It was not submitted; its scope is broader than expected. Do not fetch/delete it during this audit. The e015 refusal itself confirms non-execution and does not create a double-send ambiguity.
- **Potential double external send:** only the unresolved VT invocations `9aaec14d…` and `36114580…`. `ambiguous=false` in relay rows does not cancel their invocation status `needs_review` / `external_effect_unconfirmed`. Never retry until supported reconciliation proves the first effect state. All Bridge refusals, especially e009/e015, explicitly report pre-submit non-execution and no Bridge invocation, so no Bridge double send is evidenced.

## Journal integrity and record gaps

- `journal.jsonl`: 301 lines; all JSON parses; IDs unique; timestamps monotonic; all rows have timestamp/agent/path/response SHA; 70 POST tool calls and 231 GET calls; all POSTs carry argument SHA. Relay `ambiguous=true` count is zero. Relay response hashes/byte counts do not include response bodies.
- No previous-record hash, signed chain, investigation/run/job/correlation ID, session/model identity, or canonical DB link is present in journal records. GET request arguments are not separately hashed; actual tool arguments and bodies are absent, so invocation semantics rely on the separate analyst log. The `ambiguous` relay flag is not equivalent to the provider effect state: POST rows can have `needs_review` while `ambiguous=false`.
- `analyst_log.md` is narrative, not hash-linked to the journal; it records hindsight summaries, some timestamps were corrected/out of order, and it does not prove the database rows still match. It also cannot expose all opinion content. The checkpoint says the traces are ignored by Git. No immutable/signature chain was provided.
- The Mission Phase C contract expects durable motivation, action, result/SHA, advice, decision, and costs; the relay rows alone do not satisfy that complete audit trail. This report therefore preserves trace provenance and uncertainty instead of upgrading trace notes to canonical evidence.

## Complete POST invocation ledger

Every POST from the 301-line relay journal is listed below. `HTTP` is the relay/API envelope status; it is not provider success. `response_sha256` hashes the recorded response bytes, not a provider response unless separately stated above. Empty invocation ID means the request was rejected before a canonical tool invocation was created.


| Relay decision ID | Invocation ID | Action / outcome | State v | HTTP | Response bytes / SHA-256 |
|---|---|---|---:|---:|---|
| `a015-h1-declare` | `e79d6eaa-9414-486b-9691-03e893537ced` | `hypothesis.declare` / `succeeded` | 2 | 200 | 1432 / `0dc32236a85a5a52ec65de27753cc865577ad43429408244b0a882821de9b66a` |
| `a017-h2-declare` | `e3815f08-7776-445d-b89c-be90aa00326b` | `hypothesis.declare` / `succeeded` | 3 | 200 | 1330 / `93a78b65c30d77b31d1cd7b2697cd811422ffc060e08fc855fa7a498f9b7f1f4` |
| `a019-h3-declare` | `43d13419-caaa-4b1e-afc4-326ded8e3218` | `hypothesis.declare` / `succeeded` | 4 | 200 | 1401 / `b6535e491a6898db0a17c065cfc9d61eb8d45c7eae58c1f0a4cd26d94a0cbea8` |
| `a021-h4-attribution-declare` | `747a23b3-3f92-474a-86b1-5c27c0858d94` | `hypothesis.declare` / `succeeded` | 5 | 200 | 1410 / `a7a72d7a971b9368306d43d46b11b5343a15927edd7bb6366cc7f41e95b8af56` |
| `a023-vt-file-report-6e7d` | `9aaec14d-fe80-4987-976c-b00187b8a891` | `vt.file_report` / `needs_review` | 6 | 200 | 1080 / `0b0ef42eac879a9dcd2aa03a816ed2cb922b2bdc7c645d24e64fbc904125e56a` |
| `a027-bridge-plan-after-vt-refusal` | `0a1f6297-9bfd-4241-b4ee-966d92cacabd` | `advisor.consult` / `needs_review` | 7 | 200 | 1721 / `e8436c62ce5bc23e47ea3cb447b4e1c2cdf25beb6d7cb6cfcbd06785515dd991` |
| `a030-bridge-plan-valid-refs` | `41146d23-34bd-4a73-9d51-27471cb5847b` | `advisor.consult` / `needs_review` | 8 | 200 | 5369 / `b3341effbd6ca178cbfe15fc3821c587041d7b3ad3d16d9746b97ee66c2e7a91` |
| `a033-bridge-plan-omitting-marked-observation` | `040b0ae8-dd10-4298-96ec-fe697127c946` | `advisor.consult` / `needs_review` | 9 | 200 | 4942 / `346c581f7d31bcd29e2547aeb5291c1fbc608f83f256ab349dabb442455d406d` |
| `a041-strings-flightmanager-project` | `f3cdba7b-e731-4c15-9f88-62f50912e25a` | `local.run_command` / `policy_denied` | 10 | 200 | 1052 / `a39bb2a9f3eec8e9d2ffb0d455560e6838e3f7af42b008b4bc5ff71a2a48708c` |
| `a045-finding-sample-verification-inconclusive` | `583bd0f0-2085-448a-b2d7-9eab4e5a71d6` | `finding.propose` / `succeeded` | 11 | 200 | 1355 / `30228e2624d7d0f3828329a465a9479304517a13a332cb22fc962e4ae470ab60` |
| `a047-finding-etw-scope` | `d054e9a9-138b-49a1-8c55-3af61da3d4c1` | `finding.propose` / `succeeded` | 12 | 200 | 995 / `1cf447552e398668a4ec02366dc7741c9bbe6f9e7212f949a065dac5154dad6b` |
| `a049-finding-github-fallback` | `44d1d3fa-61a0-4bcc-a11f-f878d7b28ec4` | `finding.propose` / `succeeded` | 13 | 200 | 995 / `4e03120e7b86f7f451126a1406c60ab2bc7fdb8536870c43f9f7f581fc0ef74f` |
| `a051-finding-attribution-scope` | `d68f9a86-3242-49dd-8f52-5b96f8f04c14` | `finding.propose` / `succeeded` | 14 | 200 | 995 / `4b196012388c6a6d0f339cf1db9274ca8ec826be312321340ed4ddeebcfcf579` |
| `a057-bridge-plan-reprise` | `a1db79a4-e39d-40c4-8506-b7bdeb67190d` | `advisor.consult` / `needs_review` | 15 | 200 | 41191 / `8556bf9a6ac47f33508111c46b84501aa62d20a361be95b4a2e9a39c1d44b852` |
| `a062-bridge-plan-reprise-evidence` | `b091abe4-0612-409d-8dc4-7fca62a316dc` | `advisor.consult` / `needs_review` | 16 | 200 | 40091 / `e075821707d88a1ed7c47aec5d7e3fa39f00b7c48430143fa36191541003672d` |
| `a068-evidence-get-finding-etw` | `76280e93-a82b-4b98-b7a0-53592c9e3c2b` | `evidence.get` / `succeeded` | 17 | 200 | 1591 / `c574470f02dafb1d9302906746bbfc8439560225579614be30fc6c69166d9d43` |
| `a069-evidence-get-finding-github` | `a207c6e2-8cc5-4b61-a5e5-82c4a240e6be` | `evidence.get` / `succeeded` | 18 | 200 | 1591 / `47faf1450f699595832c353dddd416e5ae3ed5ea449680c9b62e1e20c6d164da` |
| `a070-evidence-get-finding-attribution` | `a47ea991-7f7f-426f-b58e-14e1b10f6051` | `evidence.get` / `succeeded` | 19 | 200 | 1591 / `aad24d865d6e460b7272e0beb1659a46581f6ffb7d4d80117bec16f4bbdd28fa` |
| `a072-bridge-plan-after-evidence-retrieval` | `dc0c3d0a-0c70-4cb1-a62a-6222eca6ac3a` | `advisor.consult` / `needs_review` | 20 | 200 | 39203 / `ec9767f9c9f896ae0f6e5f5838b240b9eb012c2afb4ab0af7f3601b8f1ee861b` |
| `a074-bridge-plan-using-loaded-evidence` | `41dfa6b5-3ef6-4bd6-a36e-ed3b4ac4414f` | `advisor.consult` / `needs_review` | 21 | 200 | 41063 / `738fd6ee71a066ef7fb30c5721682f58b5414b8e52ab14f23c33fec2b7bff2e9` |
| `a077-vt-file-report-6e7d-after-fix` | `—` | `vt.file_report` / `no invocation` | — | 409 | 147 / `70c4b84fa0f8e6fa0643883e2283241b787d2131efc4ac3360055a37bb312b9f` |
| `b005-vt-file-report-f5b127` | `c57f7bdb-6798-4810-8d22-ab069b7e21fe` | `vt.file_report` / `succeeded` | 22 | 200 | 1434 / `5cc3d4f4b9076e72c6c72c1b1c15ec4ae29769179b8ed752d0c4134a93633b75` |
| `b007-vt-bundled-files-f5b127` | `b0668acd-b5e5-4931-a759-25d8b6d99fee` | `vt.relationships` / `failed` | 23 | 200 | 1294 / `77411072fd60759bfee53a5838da68acd8cdeeb5a9cb45a9c72eaebf02cb2faf` |
| `b008-vt-bundled-files-f5b127` | `245c931e-8ef3-4856-889a-71f3138238ee` | `vt.relationships` / `empty` | 24 | 200 | 1209 / `2933128c36f8b0f4214cdc13c3fbe49ac70b47161c27a2a71875a7ba1269b5e1` |
| `b013-vt-behaviour-f5b127` | `c0249d3f-58c6-4e7e-84f6-6b770b3ab8ed` | `vt.behaviour` / `empty` | 25 | 200 | 1091 / `a150f8e504ccf7458ad94cf8d2b0566e89ec4abeab4f40badf826ac989cb0ecf` |
| `b018-vt-file-report-3fd810` | `f2585c57-7ec3-4643-bb32-7390a9a51f4d` | `vt.file_report` / `succeeded` | 26 | 200 | 1427 / `5312657b55131cfe6fb5af4166e532e3fe1057f21bc92764b59b336a6fb1594c` |
| `b019-vt-dropped-files-3fd810` | `0ec2e542-1db3-4017-b2b6-4ee4437c2b33` | `vt.relationships` / `empty` | 27 | 200 | 1209 / `e26d6ddccd9cad63247eb7e1b5c18d0647f644cd34f218d9950af768903a1faf` |
| `b020-vt-behaviour-3fd810` | `f668f7fb-a8c0-43fa-b172-60875487a86c` | `vt.behaviour` / `succeeded` | 28 | 200 | 2478 / `47b006c76e9dbe06325d32b053e80f72f1294c94b48b33706f27e686cfb3b434` |
| `b023-vt-contacted-ips-3fd810` | `57a35278-e2d2-42e9-9cf5-868cd4df130d` | `vt.relationships` / `empty` | 29 | 200 | 1209 / `ddcd8118fbbe4cbdfd970739d407f5f910b67060bae3c842c6eb19d7a033f083` |
| `b024-vt-contacted-domains-3fd810` | `7909cd30-bf18-4313-8934-0e3a1f1acc2b` | `vt.relationships` / `empty` | 30 | 200 | 1213 / `8808cd1d404e2a803d0cf83d2d9da6d6f9dc03dbf8b77991afb95512cd74430a` |
| `b027-revise-h1-psproxy` | `7ff6c42a-2c16-4cee-80fc-093655262fbb` | `finding.revise` / `succeeded` | 31 | 200 | 5178 / `0c5a2025475488108ed823fbf45642d9d87c846d5c3e57f6b5225b85e6a7814b` |
| `b029-vt-file-report-53f35` | `ae0631e8-1122-4475-8958-89847b123922` | `vt.file_report` / `succeeded` | 32 | 200 | 1377 / `3419483c947bb5bf266b1fa6f9a3cb64940a58687510203f7527f2123654e394` |
| `b030-vt-execution-parents-53f35` | `cd55ae40-a314-4fda-b757-0ce7c60b6276` | `vt.relationships` / `succeeded` | 33 | 200 | 1688 / `8b02e0eeff1fc0b11d5bc661866f689942d9c3a1afdd0ed8a0f69f1ac8b5e35e` |
| `b033-vt-behaviour-53f35` | `5b32f6d2-e117-4491-9fde-c4d7b1d41329` | `vt.behaviour` / `succeeded` | 34 | 200 | 2401 / `9b1565e8fe255b7845b3a76bcfcd1a834e0b30ab78bf09ab37825cc679b2f367` |
| `b034-vt-contacted-ips-53f35` | `d5c1e48e-2ae1-4e97-925f-9b15b191b7f2` | `vt.relationships` / `succeeded` | 35 | 200 | 1354 / `9f758248c884a4b67688750923c6bb4a0a15cd3547db19f408e06b2f1ab375a8` |
| `b035-vt-contacted-domains-53f35` | `d13562ca-14d2-45b7-9ecc-80e14832aa7a` | `vt.relationships` / `empty` | 36 | 200 | 1213 / `acf2d7605033a7054c7a4a3e51c97fa641a22010413b8dafb66ae7be85b2422e` |
| `b042-revise-h1-runtimebroker` | `b26d4005-169f-4bfc-8754-78b25017fe21` | `finding.revise` / `succeeded` | 37 | 200 | 9220 / `75d746d268d433c8b041181882782ec6ab11ad18578a1725e031ca11b714e86e` |
| `b054` | `7ded4fb3-369b-4609-a195-e9a2435e1425` | `vt.file_report` / `succeeded` | 38 | 200 | 1375 / `f0f3b350a3c435fca546a1edc62e317a0fa84e0e78e6400af98a36bae91fe99e` |
| `b056` | `21532a81-cbea-49e6-a395-db198032e5f3` | `vt.relationships` / `succeeded` | 39 | 200 | 2321 / `9ee4385d207326e244245b30a598c82d5852e3c33c3bf8000bdc3e4a32d41152` |
| `b057` | `90d586c2-ff25-4ef5-8cca-398023ad3c25` | `vt.relationships` / `succeeded` | 40 | 200 | 1356 / `df35cf00a7695a06f2bff49b2d5d8d82f9db2281509d3256ab1df3c4a33bdf9b` |
| `b058` | `d044004a-f745-4bb0-a64e-a4b5824a170d` | `vt.behaviour` / `succeeded` | 41 | 200 | 2472 / `c85201c19a4a1181ec6030fd049a1aaff3a37ff3f1e4dd135b933227b6eaa4e2` |
| `b059` | `2709873d-4f55-43f8-b112-8fdc20e97555` | `vt.relationships` / `succeeded` | 42 | 200 | 1352 / `137c3b4f804f219f1f7bb811218fe5a10ef8218a3a3633f797c3ee05667bae9a` |
| `b060` | `fcc412b7-45f4-4a5a-b692-2a9e986cbe66` | `vt.file_report` / `succeeded` | 43 | 200 | 1415 / `39b8564f84bfa15e5650b4e8469a5bb0d8a0f6ecfd5815faa49df86f122c8d4e` |
| `b061` | `e563cb2f-e9af-4abe-a6f6-18eb285263a6` | `vt.relationships` / `succeeded` | 44 | 200 | 3090 / `078f3941123a51ad52cae77f508da267bd6b540cdc8a4d6afc48b8091f50ac2e` |
| `b065` | `d9ea2613-d2de-4566-a527-32208f4bfd2a` | `vt.file_report` / `succeeded` | 45 | 200 | 1410 / `f3d05daaabeda11411459153d5ba2340a77bf2d415976b1efc9e2d75c2053907` |
| `b066` | `163f8c2a-6ecd-4808-9d40-0faad1bbe0a1` | `vt.relationships` / `succeeded` | 46 | 200 | 3101 / `1ef9b6a086a45c6c7497c73a9cdcf4023067b41b9bf5abddd9dd006b7fe4dffc` |
| `b067` | `10dca30b-2a39-4212-82ec-c396dbd2ab69` | `finding.revise` / `failed` | 47 | 200 | 1262 / `93b6fd6b6d6079fc1394203a393621abf9d0659e6ba49e1f56632626bfe105e6` |
| `c013-bridge-plan-initial` | `8befcade-0de7-42f7-97b3-f4cb5ae93ec9` | `advisor.consult` / `needs_review` | 48 | 200 | 54893 / `8eb2b71ca5462555a8d40fa2b1b7f22c2f2d3617b441c6c226b935e17f8d7139` |
| `c016-vt-file-report-6e7d` | `—` | `vt.file_report` / `no invocation` | — | 409 | 210 / `37b301abd54c5fcb4cb92faf096c33d464afb3249b1a0ea95488ac62d66625ba` |
| `c022-download-flightmanager-csproj` | `695ba7b4-0fed-42be-aa45-90a7b6014114` | `vt.download_sample` / `succeeded` | 49 | 200 | 1679 / `e84a53bfc2ac76381137e87277ba5c6bb51c31dbea5b2e4a7a333e1f3c5c2557` |
| `c023-static-analyze-flightmanager-xml` | `5cc73343-2997-4484-b4cb-5f621e4a61a7` | `sample.analyze` / `partial` | 50 | 200 | 20155 / `219e6ec7264cdcb00d8defd13f528e8da5d1089cfabb0ce38e39fa21fef6f912` |
| `c029-download-psproxy` | `73e9a9fb-ec22-4b94-ac37-00eac5d86ed3` | `vt.download_sample` / `succeeded` | 51 | 200 | 1679 / `d51035456a14271940d5d6adab90e2382b5d7de73d966fa3ef618543d206345c` |
| `c030-strings-psproxy` | `f9a4a61b-5389-4ba1-ad20-8b57491603cc` | `strings.list` / `succeeded` | 52 | 200 | 2994 / `22642a109f99505b2c04cd63efc414ef59417b528e0355bcdf245223cf67a4f9` |
| `c035-revise-h1-static-project` | `6b89f4e2-c18a-4cc2-b814-d4ef4fde3b07` | `finding.revise` / `succeeded` | 53 | 200 | 9100 / `324aaece2112dc8169bd92e305331ff1f6354a025f1844c2379f9ebcdd0812b0` |
| `c040-compose-internal-opinion` | `84c513d5-dc3e-4eb3-9bcd-3c464cac3c38` | `opinion.compose` / `succeeded` | 54 | 200 | 9102 / `51a5077c9dfc146ae2d4691d36d0d132ba2a797ecb4819a672fe2f540e6cce00` |
| `d008-bridge-plan` | `233eff34-dd4a-4eb6-ba77-d6e15499e453` | `advisor.consult` / `needs_review` | 55 | 200 | 74793 / `49b4730aa2fe8b4245afe508d856d1341ab3f22db2fa728712c2926611f2fc6c` |
| `d014-bridge-plan-corrected` | `51701daa-094c-4d6f-aa30-f44a517e2aec` | `advisor.consult` / `needs_review` | 56 | 200 | 71221 / `23f884e247ecf008430c8b67f44e63c1d973cc5894c2d4eec37812b3da692ec3` |
| `d021-bridge-plan-after-fix` | `b16c1170-f65e-45e8-9d55-a46b148254c1` | `advisor.consult` / `needs_review` | 57 | 200 | 71221 / `c3a41e7290f2faeaf2298d10bda7b4b1eb6a7e6aba02d4b688068ed8b16a0765` |
| `d030-zip-bundled-files` | `36114580-ccbb-47a6-b170-3e9fa675e1d5` | `vt.relationships` / `needs_review` | 58 | 200 | 1103 / `93de8e90ec970b04f60b7eeb676678d62009d95e7098275363429fd7c30d7895` |
| `d033-download-zip-h1` | `92185481-78c1-48a0-8002-f9a57c156eef` | `vt.download_sample` / `succeeded` | 59 | 200 | 1681 / `cfe2764f628a596ba553a2d6ba06f06a0a2bcd3585a33dd8adf17da14d4c96e1` |
| `d034-analyze-zip-h1` | `313f12ce-d70f-4327-955e-f72af3c55d4a` | `sample.analyze` / `partial` | 60 | 200 | 13704 / `e2f546cc7026a3d2855c363671e1cf70398fcd2acf6e7a637a8c3c047733176c` |
| `d038-zip-raw-strings` | `015affb5-961a-4aa3-9312-79731a759303` | `local.run_command` / `partial` | 61 | 200 | 5723 / `6f7687a922318c4a5304245e8f31b3b61d8f2bcb7b462f746f4297612064afb7` |
| `d039-zip-central-dir-window` | `461bb67b-3abe-46d4-be38-fb0b403c8a94` | `local.run_command` / `succeeded` | 62 | 200 | 5211 / `f2c2810ae97a7b7cb79b9be13917a0cf3f5469ca66f2a32cbfb5f6e8420c59cd` |
| `d047-zip-central-dir-start` | `dd4e3430-d1ff-4d20-a58f-4ef5a6ff9bc8` | `local.run_command` / `succeeded` | 63 | 200 | 5211 / `ee2e2240294a98751fcac3c2b337e2bfe6f548ed094fa5a23c4497c489cd9e46` |
| `d053-revise-h1-zip-manifest` | `6dda78e2-c74f-4e2e-bec3-326aaeba7cc6` | `finding.revise` / `succeeded` | 64 | 200 | 19398 / `028d6d9eec0d7c624bdde42aae83a49bc01751ade15ec595e54739ba97fba43e` |
| `d057-revise-opinion-after-zip` | `999f099b-d080-4eb1-b417-be0010267e7f` | `opinion.revise` / `failed` | 65 | 200 | 964 / `8edd555e912d1b275f1ec762be54bf283a5d74780a9e23553d7605122d560781` |
| `d061-revise-opinion-citations-corrected` | `f0770b68-c419-40da-a2a5-264cb665e65e` | `opinion.revise` / `failed` | 66 | 200 | 964 / `21aa32e95c23da9664f44a413157f351e86a3ffe00e3cd25daa849de4cc4c502` |
| `d064-revise-opinion-minimal` | `5300d85f-ca54-45f9-894c-fdf9ef656ec4` | `opinion.revise` / `failed` | 67 | 200 | 964 / `cba6cd29fea61c3288ee589279d3979b9bbd62e5fdcac2e1bc0fcaf76e8352cc` |
| `e009-bridge-plan` | `bc4acebb-2d66-4543-aeba-fc12fd64aa30` | `advisor.consult` / `needs_review` | 68 | 200 | 83409 / `b8f85acd24f94f5305007ecb759428149f5ca8f44aa5e7ad1e63ce9b93083166` |
| `e015-bridge-plan-corrected` | `11db4592-7107-47e5-955d-e78c27ecd0e1` | `advisor.consult` / `needs_review` | 69 | 200 | 131665 / `6096016808c098f6c9991ed9ea5b8c5a9eb929cf3f64502e729342447033b9ef` |

## Audit conclusion

Checkpoint-1 is **PARTIAL / BLOCKED on canonical reconciliation**, not PASS. Trace evidence shows useful, bounded CTI pivots and cautious current findings, but no Bridge consultation reached the provider, the opinion body is unreviewable, the current database state could not be independently checked, two VT effects remain unresolved, and reservations remain reported as leaked. The next permissible audit action is a supported read-only canonical DB snapshot plus human reconciliation of the two unresolved VT invocations; do not replay either effect or resume Bridge until the authority binding is fixed through an authorized path. No tests were run and no external service was contacted.
