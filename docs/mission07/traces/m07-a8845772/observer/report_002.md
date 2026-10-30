# Mission 07 — independent audit, checkpoint 2

**Scope:** CTI-ANALYST-6 on investigation a8845772-5025-4702-8850-2b6be776855b, explicitly marked ORCHESTRATOR smoke requests, Analyst 6's log from its first entry, the checkpoint, and c6-* relay records. No examination of the prohibited investigation or secret/runtime directories was performed. No tool POST or CTI research was issued.

## Blocking state and conclusion

- The canonical investigation is still running, operator-led, working-state v115 / SHA-256 7f97cb16a7e054b5397b6dfa56fe5f4d47fe7c254475638f49934b52dbba7f32, with no active jobs. Current findings page contains five findings and all are qualified; none is supported or refuted. Current opinion 128f991d-09c9-5cb5-905c-d0c044bfff00 is draft v2 / SHA-256 a7baeab86dcd36fab671aead80fc154989d42770f23eae06d62bb1fdc856a300.
- **Bridge review of opinion v2 is incomplete.** Corrected request c6-104 was eventually processed. Invocation fcceac6c-6740-494a-b163-dd25bba5e651 is partial, has Bridge invocation 9f6ec75c-d26f-45d8-b938-9e74ba6f903d, consumed one Bridge turn, and returned no usable review units (U1–U4 unprocessed). Do not describe v2 as Bridge-reviewed. No advice was accepted from this response.
- The checkpoint's 16:47–16:48 note that c6-104 had no local receipt accurately described the files then visible, but it was not proof of non-transmission. The analyst appropriately stopped and did not duplicate the queued request, and explicitly allowed for late pickup. The relay completed it at 2026-10-28T16:50:15.599668Z; an eventual claim “no send occurred” would be false. Treat the former conclusion as stale, with relay-state visibility as the defect, not as an unreasonable stop decision.
- No duplicate external review occurred: c6-094 failed before transport with zero turns; corrected c6-104 was submitted once. c6-084 was a separate pivot consultation. No resend is warranted for c6-104 because its existing response is partial and unusable.

**Canonical reads:** .autowork-runs/m07/outbox/obs2-1.json (/operator-status); obs2-3.json (/envelope); obs2-4.json (findings section); obs2-5.json (current-opinion section). The direct /findings read (obs2-2) returned HTTP 403 operator_client_scope_denied; the versioned operator context section succeeded.

## Significant decisions

### D1 — establish whether RuntimeBroker has inspectable managed code

- **DECISION_ID / INVOCATION_ID:** D1; methods.list cda7219c-3f1f-4081-83d2-9c4f55185492, strings.list cdd72e30-f84a-47f0-b3ad-9bfbecbe9c72, references.imports 387d70d6-46d2-4a68-9e7b-4d81a9a611e8, cbd289b2-413d-4004-b0da-f3bdcacc1a2a, 4cadc255-5af3-43de-93cf-01dc666ba3fa, eb48cb2e-d286-4467-b1df-80799b0f1494, bd31ad9f-e47c-4f9f-bc85-4602dfda270e; types.list f954fac0-5b4f-497a-b333-02cc91022e03.
- **INFORMATION_AVAILABLE_BEFORE_DECISION:** H2 concerned an ETW setting in a .NET/AppDomainManager path. Prior capa/FLOSS output was generic or inconclusive; sample execution was prohibited.
- **ACTION_CHOSEN:** Enumerate managed methods, the user-string heap, imports and declared types in bounded pages; use metadata only to choose focused method bodies.
- **EXPECTED_INFORMATION_GAIN:** Find a candidate AppDomain/configuration method and determine whether an ETW write is present, while separating import/capability labels from executed behavior.
- **OBSERVED_RESULT:** ILSpy 9.1.0 identified an IL-only managed image and 207 obfuscated methods. The user-string heap was empty. The complete 824-line reference inventory exposed AppDomain, reflection, WebClient, crypto, registry, process and WMI APIs; no ETW/EventSource- or GitHub-specific API reference appeared. API imports did not establish call sites.
- **RELEVANCE_ASSESSMENT:** Appropriate high-value static discovery. Negative statements were bounded to the heap, completed metadata inventory and later inspected bodies. No execution or network contact occurred.
- **ALTERNATIVE_IF_MATERIAL:** Stop at inconclusive metadata if method bodies were unavailable; this was not needed because ILSpy methods were available.
- **SEVERITY:** Low; proportionate and successful.
- **CANONICAL_EVIDENCE_REFS:** observations 5f80a22e-4396-564c-a497-9746c4d8fdd5, 51ff9da3-5b5b-5e51-928c-d3f694e59373, d08b9c53-a8d3-5787-b015-ea617c7bf696, a8419aaa-8e1a-5294-a90f-9a34284becb0, 8f7e6e07-11a2-5dbc-bf7c-d30b291c85bd, 5f4c7730-dc77-52db-b574-7bd74c77f430, b2664ea1-8a3d-5b25-8d48-a85ee33bb8f9, b6d6d649-255b-578e-9112-de862465640f; Analyst 6 log lines 512–549.

### D2 — decompile only methods that discriminate the ETW and network claims

- **DECISION_ID / INVOCATION_ID:** D2; method.decompile b62b9e4d-e07c-49e1-80f7-61e8365b2ed7, 6bdeb0f5-0bd7-4101-a0a7-3315ee7f6655, 3bd2dbf5-c712-462d-8844-ebf4b43ccba1, 1a3be4a5-99ee-4d97-bd26-d43f1cfa2876, 761b31cd-ddeb-4980-9007-0bc44279d26f.
- **INFORMATION_AVAILABLE_BEFORE_DECISION:** Metadata showed an AppDomainManager candidate, two timer callbacks, and generic WebClient/crypto APIs, but no semantics or call paths.
- **ACTION_CHOSEN:** Decompile the entry method, default-domain helper, both callbacks, and one WebClient-shaped method.
- **EXPECTED_INFORMATION_GAIN:** Determine whether the alleged ETW configuration or periodic HTTP polling appears in the bodies; characterize the generic network path without claiming active C2.
- **OBSERVED_RESULT:** InitializeNewDomain sets current directory from the AppDomain base directory and dispatches to a helper. The helper installs handlers, sets HTTP service-point options, creates a notification icon, starts 63-second and 120-second timers, and enters a WinForms loop. The inspected callbacks perform local path/visibility and file/registry checks, not HTTP polling. RID20 contains generic WebClient URL-fragment, download/response-parse/Base64 and conditional-upload logic; endpoint, callers, response schema and runtime behavior remain unknown.
- **RELEVANCE_ASSESSMENT:** Inferences are proportionate. Static code presence is explicitly separated from invocation, runtime effect, C2 and campaign linkage. This supports a qualified capability finding, not an observed network behavior.
- **ALTERNATIVE_IF_MATERIAL:** Inspect call sites or obtain a separately authorized runtime observation; neither was available or performed.
- **SEVERITY:** Low; strong technical work with appropriately bounded conclusions.
- **CANONICAL_EVIDENCE_REFS:** observations ff209343-f085-5dae-a721-79b51b079663, 60e6a38a-ac6c-5896-a080-c7de01da2dda, 0d439fa8-94f0-579e-ba30-808bc30a5034, f47a08b3-43e3-5397-bae3-d7fb0cff5886, efe8966c-957c-5922-a009-c23aa00f9082; Analyst 6 log lines 551–574.

### D3 — use FLOSS and inspect the static string decoder, then stop before decoding values

- **DECISION_ID / INVOCATION_ID:** D3; no new FLOSS invocation (reused sample.analyze observation); method.decompile 617ab87e-71f6-4305-b35b-87389335b78a, 4150195e-8b03-4533-b1eb-48c40635e597, a2f0ea79-0715-4515-8587-0728bfe9d5e4, 3c3a5cda-56b3-43cf-b023-796ff1a0032a, 54aa58d6-499c-4b6e-b604-73b56a977283.
- **INFORMATION_AVAILABLE_BEFORE_DECISION:** The WebClient path used obfuscated helper strings; no readable endpoint or marker had appeared in the metadata or user-string heap.
- **ACTION_CHOSEN:** Reuse the recorded FLOSS output, decompile the string getter/decoder and complete the method inventory; inspect the table initializer statically, but do not execute or print decoded material.
- **EXPECTED_INFORMATION_GAIN:** Explain whether the empty string results were meaningful and whether the decoder exposes a bounded static lead.
- **OBSERVED_RESULT:** FLOSS succeeded with 128 strings but no readable GitHub/ETW/config markers. The getter accesses a cached string-table value; the decoder is called with fixed integer inputs; the initializer populates a byte array and applies an index-based XOR transform. No table values were decoded or recorded.
- **RELEVANCE_ASSESSMENT:** Correctly treats FLOSS/heap absence as inconclusive under obfuscation. Stopping avoids producing opaque endpoint/key material without resolving archive membership or campaign linkage.
- **ALTERNATIVE_IF_MATERIAL:** Continue only for an explicitly justified static discriminator; the analyst instead prioritized exact archive-member hashes after Bridge advice.
- **SEVERITY:** Low; good restraint.
- **CANONICAL_EVIDENCE_REFS:** observations c79308e4-978a-56ae-a88f-721fd3093f5b, 391f3363-6389-54f2-8350-6329992f612c, 3afcb7bd-4bd9-5803-b5a0-fb0a8db181a1, 0b354777-c78d-59a2-a586-ab04668d7d96, c88fe8d9-b96c-5c96-bd86-ce3a7bdcea1a, d0a069d2-22b7-5169-afe6-a45501a917bd; Analyst 6 log lines 576–600.

### D4 — Bridge pivot strategy consultation

- **DECISION_ID / INVOCATION_ID:** D4; c6-084, invocation f48e2c25-1c31-48d1-8f1a-6c89f26914a9; Bridge invocation 6e75af9a-e583-4c7d-b4b3-9ea39c0bd1a0.
- **INFORMATION_AVAILABLE_BEFORE_DECISION:** Static CIL covered the AppDomain helper/timers, generic WebClient method, obfuscated table and bounded FLOSS/import results. Archive member names were known, but their bytes were not.
- **ACTION_CHOSEN:** Ask which claims were directly supported, what remained unverified, and for one bounded, falsifiable static pivot distinguishing archive/DLL relation, ETW scope, or GitHub Issues fallback. Dossier: eight observation references covering entry/helper/timers, WebClient body, FLOSS, decoder and initializer; no raw sample bytes.
- **EXPECTED_INFORMATION_GAIN:** Select a falsifiable, non-executing discriminator instead of expanding speculative string analysis.
- **OBSERVED_RESULT:** advisor.consult was partial but structured output was usable; summary was empty (summary_missing_or_invalid), provider usage unavailable. Bridge advised exact SHA-256 comparison of Resources/RuntimeBroker.dll and Resources/PsProxy.dll inside the acquired ZIP against the standalone candidates. An exact match would establish identity for that archive; no match would exclude those exact candidates only. Generic HTTP/imports do not prove active C2. One turn consumed; cost record says 364 tool-seconds, 406,580 context bytes / 101,645 estimated context tokens.
- **RELEVANCE_ASSESSMENT:** Advice is untrusted and adds no evidence. Analyst ACCEPTED the discrimination criterion, ADAPTED/DEFERRED execution because no sanctioned archive extraction/member-hash tool was available. This is justified; VT needs_review output was correctly not replayed and no improvised extraction was attempted.
- **ALTERNATIVE_IF_MATERIAL:** Use an authorized static member-hash capability after confirming availability; until then retain qualified archive association.
- **SEVERITY:** Low; one partial consultation with structured advice still useful.
- **CANONICAL_EVIDENCE_REFS:** invocation f48e2c25-1c31-48d1-8f1a-6c89f26914a9; Bridge invocation 6e75af9a-e583-4c7d-b4b3-9ea39c0bd1a0; output SHA bd02a44428b5d81818c63c60f64ee4b12de1a58c674e019089a59d1212905309; evidence observations 2b21c0d4-3241-5f62-9ad0-675fa86ec8fa, efe8966c-957c-5922-a009-c23aa00f9082, d0a069d2-22b7-5169-afe6-a45501a917bd, ff209343-f085-5dae-a721-79b51b079663; log lines 610–614.

### D5 — add a qualified network-capability finding, revise H2, draft opinion v2

- **DECISION_ID / INVOCATION_ID:** D5; finding.propose 80fe0ccf-91de-4bf2-87e9-2dd9e4f703c5; finding.revise a21e0cb0-64e6-48a5-b463-674092ca8d9a; opinion.revise 71fcf2dd-275d-4ab6-8350-2a70e45692ae.
- **INFORMATION_AVAILABLE_BEFORE_DECISION:** Direct static evidence of generic HTTP transfer code, but no call site, endpoint, archive-member identity, runtime behavior or ETW write in the inspected bodies.
- **ACTION_CHOSEN:** Retain the new capability as qualified; revise H2 with bounded method/import coverage; use inference/hypothesis blocks in a complete opinion replacement and request review before finalization.
- **EXPECTED_INFORMATION_GAIN:** Preserve verified method-level facts and make limitations clear without converting them into facts about execution or attribution.
- **OBSERVED_RESULT:** New finding 91d9ff4d-193a-5773-96b0-56bc3b1efd97 is qualified, citing RID20 observation only. H2 is current v2 a507ceb9-1f82-51c3-bef6-f01f63fc94ad, also qualified. Opinion v2 SHA a7baeab86dcd36fab671aead80fc154989d42770f23eae06d62bb1fdc856a300, four blocks: key findings, claim verification, additional samples, uncertainties/next leads.
- **RELEVANCE_ASSESSMENT:** All five current findings are qualified: H1 f6f0a34e-8207-5e1c-9040-b17cb49b95d8, H2 a507ceb9-1f82-51c3-bef6-f01f63fc94ad, H3 dd157c98-921e-5194-b863-f00fc899ebc8, H4 746907e4-ccde-53a5-b073-0e189976758c, H5 91d9ff4d-193a-5773-96b0-56bc3b1efd97. This is justified: H2/H5 have direct static evidence but not runtime proof; H1 has VT/source/central-directory association but no internal member hash; H3 has no loader/traffic proof for GitHub fallback; H4 remains source-reported attribution. Both H3 and H4 have no observation_refs in the current finding records, so their qualified state is a conservative non-verification, not a conclusion established by direct sample evidence. No supported or refuted assessment is justified by the inspected evidence.
- **ALTERNATIVE_IF_MATERIAL:** Keep opinion v1 or defer drafting; the analyst instead produced a qualified draft and explicitly withheld approval/publication/release.
- **SEVERITY:** Medium until review; draft remains unapproved and unpublished.
- **CANONICAL_EVIDENCE_REFS:** finding refs above; H2 observations ff209343-f085-5dae-a721-79b51b079663, 60e6a38a-ac6c-5896-a080-c7de01da2dda, 0d439fa8-94f0-579e-ba30-808bc30a5034, f47a08b3-43e3-5397-bae3-d7fb0cff5886, d08b9c53-a8d3-5787-b015-ea617c7bf696, a8419aaa-8e1a-5294-a90f-9a34284becb0, 8f7e6e07-11a2-5dbc-bf7c-d30b291c85bd, 5f4c7730-dc77-52db-b574-7bd74c77f430, b2664ea1-8a3d-5b25-8d48-a85ee33bb8f9; opinion metadata read obs2-5; log lines 616–630.

### D6 — first review_text attempt and correction

- **DECISION_ID / INVOCATION_ID:** D6; c6-094, invocation 3d9410f1-dd42-4273-ada8-13e62e35f653.
- **INFORMATION_AVAILABLE_BEFORE_DECISION:** Opinion v2 was current; requested review covered four blocks (HTTP capability, ETW/GitHub limits, archive identity, attribution), citing seven analysis observations and five qualified findings.
- **ACTION_CHOSEN:** Submit one review_text request challenging overclaims, missing citations, qualification/ref mismatches, and runtime-vs-code distinction.
- **EXPECTED_INFORMATION_GAIN:** Independent challenge to the exact v2 draft before final revision.
- **OBSERVED_RESULT:** Failed at pre_transport_validation, editorial_unit_target_invalid, confirmed_non_execution=true, Bridge invocation null, zero turns. Each body unit used anchor “opinion-v2”; current validator requires anchor to be null for form=body (investigation_advisor_tools.py:4813, 4821–4829). This is input/schema validation, not a policy refusal or Bridge answer.
- **RELEVANCE_ASSESSMENT:** Avoidable but low impact. Analyst corrected unit anchors to null and submitted once; no external send occurred for this failed attempt.
- **ALTERNATIVE_IF_MATERIAL:** Validate body-unit targets against the tool schema before submission.
- **SEVERITY:** Low.
- **CANONICAL_EVIDENCE_REFS:** invocation 3d9410f1-dd42-4273-ada8-13e62e35f653; failure observations 7b0ca083-cb49-59d2-8653-44073cf2bfcd, 2ff5a529-4431-510d-9f42-7d0abe8cbac6; CodeGraph-confirmed source lines above; log lines 632–635.

### D7 — stop on pending corrected review; reconcile the late completion

- **DECISION_ID / INVOCATION_ID:** D7; request c6-104, invocation fcceac6c-6740-494a-b163-dd25bba5e651; follow-up status request c6-105.
- **INFORMATION_AVAILABLE_BEFORE_DECISION:** At the analyst's 16:47–16:48 checks, c6-104 and c6-105 were visible only in the inbox; no processed/outbox result was visible. The relay is single-threaded and a late pickup was explicitly possible.
- **ACTION_CHOSEN:** Stop the review path and do not resend while the first request might still be processed.
- **EXPECTED_INFORMATION_GAIN:** Wait for the original invocation's persisted result so the opinion is not reviewed twice and status is not guessed from an inbox snapshot.
- **OBSERVED_RESULT:** The relay later processed the POST once. Canonical status v115 records it partial, Bridge invocation 9f6ec75c-d26f-45d8-b938-9e74ba6f903d, provider-usage observation 449ed0d4-6388-5d52-85df-5976b3142d8c (usage_unavailable), estimated context 178,514 bytes / 44,629 tokens, 331 tool-seconds, one Bridge turn. Parser output says usable=false; warnings: wire_header_missing, wire_prose_ignored, type_missing, status_missing, review_unit_missing; U1–U4 remain unprocessed. One send occurred but no usable advice was produced.
- **RELEVANCE_ASSESSMENT:** The no-retry decision was correct and prevented double-send risk. The local “no receipt” state was evidence of unresolved relay work, not proof of no transport. Checkpoint/log now need reconciliation; Bridge review remains incomplete.
- **ALTERNATIVE_IF_MATERIAL:** Record the review as DEFERRED and leave the draft unreviewed. Any future consultation would require a new, separately authorized action after reconciling this invocation; do not replay c6-104.
- **SEVERITY:** High for review completion and record freshness; no duplicate or publication harm observed.
- **CANONICAL_EVIDENCE_REFS:** invocation fcceac6c-6740-494a-b163-dd25bba5e651; Bridge invocation 9f6ec75c-d26f-45d8-b938-9e74ba6f903d; provider observation 449ed0d4-6388-5d52-85df-5976b3142d8c; status GET c6-105 and independent status GET obs2-1; current state v115 / SHA above; Analyst 6 log lines 632–640 and checkpoint addendum.

## Bridge consultation ledger — Analyst 6

| Request | Question and dossier | Persisted status / advice | Analyst disposition |
|---|---|---|---|
| c6-084, f48e2c25-1c31-48d1-8f1a-6c89f26914a9 | Challenge direct static support vs unresolved ETW/GitHub/runtime claims; choose one bounded, falsifiable static pivot. Eight evidence refs: AppDomain/helper/timers, WebClient body, FLOSS, string decoder and initializer; no raw bytes. | Partial, usable=true, summary missing/invalid; advice: exact hash comparison of the two DLL members inside the archive to standalone candidates. Match establishes exact inclusion; no match excludes only those candidates in that archive. Does not establish C2, ETW scope or execution. | ACCEPTED criterion; ADAPTED/DEFERRED execution for lack of sanctioned member-hash tool. Justified; advice is not evidence. |
| c6-094, 3d9410f1-dd42-4273-ada8-13e62e35f653 | Review four v2 blocks for overclaims, citation/ref qualification, code-vs-runtime, archive identity and source-only attribution. Seven observation refs, five finding refs. | Failed before transport: editorial_unit_target_invalid; Bridge invocation null; zero turns; no advice. | DEFERRED; fixed unit anchors once. Justified. |
| c6-104, fcceac6c-6740-494a-b163-dd25bba5e651 | Same review question and dossier; four body units corrected to null anchors. | Submitted once, partial, usable=false; all four units unprocessed; provider usage unavailable; one turn consumed. No usable review advice. | DEFERRED; no advice accepted/rejected because none was parsed. Correct not to resend; opinion remains unreviewed. |

## Static-analysis quality: RuntimeBroker.dll and PsProxy.dll

- Analyst 6's new sample work is on RuntimeBroker only. ILSpy method/type/import enumeration was bounded and followed by targeted CIL decompilation; obfuscated functions were not treated as decoded behavior. Method-level findings distinguish code presence from call sites, execution, active endpoint, sample-to-archive identity and campaign linkage.
- Analyst 6 reused existing capa/FLOSS observations rather than rerunning them. Capa's 37 broad capability labels were treated as leads. FLOSS's successful 128-string output had no readable GitHub/ETW/config markers, correctly recorded as inconclusive because strings were obfuscated and the managed string heap was empty.
- No Ghidra request was made by Analyst 6 and no Ghidra output is cited in H2/H5 or opinion v2. ORCHESTRATOR smoke requests for ghidra.list_functions had two unavailable results followed by two succeeded results; they are not evidence of runtime behavior and are not attributed to Analyst 6.
- PsProxy was not decompiled by Analyst 6 in this continuation. Earlier context had an empty .NET user-string heap for the VT-named PsProxy sample; that bounded parser result does not refute its behavior. Exact archive-member SHA comparison for both DLLs remains unperformed. H1 appropriately remains qualified.
- No sample execution, network/C2 contact, or new VT/Shodan action occurred in Analyst 6's 24 POSTs; only static local tools, finding/opinion mutations and the two actual Bridge turns were used.

## Findings and opinion review

- **H1 f6f0a34e-8207-5e1c-9040-b17cb49b95d8 — qualified:** documented names/VT relationships and a complete archive central directory support an association, but not exact member identity or runtime execution. Member hashes are missing. Appropriate.
- **H2 a507ceb9-1f82-51c3-bef6-f01f63fc94ad v2 — qualified:** direct CIL and complete import coverage bound the methods examined; no ETW write was found in them. This does not prove process configuration or global telemetry effect. Appropriate.
- **H3 dd157c98-921e-5194-b863-f00fc899ebc8 — qualified:** source reports GitHub Issues fallback; no loader code or traffic independently verifies it. Conservative as a non-verification, but not independently justified by direct sample evidence: current observation_refs are empty. Absence of markers is not a refutation.
- **H4 746907e4-ccde-53a5-b073-0e189976758c — qualified:** attribution remains source-reported; no independent group evidence. Conservative as a non-verification, but not independently justified by direct sample evidence: current observation_refs are empty.
- **H5 91d9ff4d-193a-5773-96b0-56bc3b1efd97 — qualified:** RID20 CIL directly shows generic HTTP request/response and conditional-upload code; call sites, endpoint and execution are unknown. Appropriate rather than supported as operational behavior.
- **Opinion v2:** The four blocks stay within those limits: generic WebClient code is described as code; ETW/GitHub negatives are explicitly bounded to examined methods/metadata; archive identity is explicitly unresolved; attribution is not asserted. No material overclaim found. All blocks use qualified finding references and inference/hypothesis framing, not unsupported fact claims. No approval or publication occurred.
- **citations_invalid diagnosis:** Three earlier opinion revisions (before Analyst 6's first entry) failed with generic citations_invalid and did not identify a bad reference. The checkpoint's explanation—that factual blocks lacked supported findings—is plausible but not proved by the returned error. Analyst 6's all-inference v2 with qualified finding refs avoids the suspected mismatch and the canonical revision succeeded. This is separate from c6-094's editorial_unit_target_invalid input error and c6-104's unusable Bridge wire response.

## Repeated calls, refusals, budgets, journal integrity and friction

- **Analyst 6 request ledger:** 121 journal entries (97 GET, 24 POST); every c6 request currently has both processed/ and outbox/ records. The journal captures request metadata, not eventual HTTP/Bridge completion, so it cannot alone prove whether an external send occurred. The user signal recorded in the 16:48 analyst-log note has no corresponding user-action journal row; ORCHESTRATOR smoke POSTs are separately labeled. The checkpoint was not refreshed after c6-104's 16:50 completion.
- **Repeated/inefficient reads:** c6-071 /findings and c6-080 direct opinion GET both returned 403 operator_client_scope_denied; context-section GETs were available. c6-072/073 returned 409 stale_working_state; c6-074/075 returned 422 operator_context_offset_cursor_unsupported after numeric cursor=0; c6-100 returned 404 operator_context_section_unknown for opinion. These were corrected by using the versioned section route and current_opinion. c6-077 and c6-078 are identical opinion_notes reads; one was redundant.
- **Refusal vs defect:** No Analyst 6 policy refusal is evidenced. The two 403s are client-scope denials; c6-094 is pretransport schema validation; c6-104 is a partial response/parser failure. Separate ORCHESTRATOR smokes include Shodan host policy_denied twice (Policy does not allow shodan.host) and a later needs_review result stating Shodan refused authorization for that capability; these are not Analyst 6 actions and must not be treated as successes. Smoke Bridge requests had advisor_input_rejected, required_canonical_evidence_not_in_payload (including needs_review/failed cases), and one partial result; only the partial result consumed a Bridge turn. A smoke sample.analyze call first failed because required purpose was missing, then returned partial. Ghidra was transiently unavailable before later list-functions successes.
- **Budget:** At c6-083, Bridge budget was limit 200, consumed 2, reserved 16, remaining 182. c6-084 consumed one turn (364 tool-seconds); c6-094 consumed zero; c6-104 consumed one turn (331 tool-seconds). Current envelope/status: consumed 4, reserved 16, remaining 180 Bridge turns; 1,333 tool-seconds consumed, 57,840 reserved, 286,427 remaining. Context was 913,535 bytes consumed / 2,039,728 reserved / 64,155,601 remaining. Provider usage is unavailable for both Analyst 6 Bridge submissions, so provider token/currency usage is UNKNOWN; context-token figures are estimates from bytes.
- **Observed friction/defects:** Direct findings/opinion GETs are scope-denied while versioned context sections work; section names/cursor semantics caused avoidable reads; body review units require null anchor (source line 4821), but the error was only surfaced after POST validation; the corrected review response parser could not recover a typed header/status/unit from the Bridge wire response; and the relay exposed no clear in-flight state before late completion. These are access/schema/transport observability problems, not evidence that the underlying CTI claims are false.

## Reproducible read-only follow-up

1. Keep opinion v2 classified as draft/unreviewed and do not retry c6-104.
2. If a later authorized reviewer needs Bridge feedback, first reconcile the existing partial invocation and create a new, distinct request only under applicable approval. Otherwise record manual review separately; do not convert this partial response into approval.
3. Preserve H1 as qualified until exact archive-member hashes are available from an authorized static extraction path; do not infer identity from central-directory names.
