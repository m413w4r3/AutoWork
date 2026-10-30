# Final Checkpoint C — Mission 07

Date: 2026-10-29. Agent: CTI-OPERATOR-C. Investigation: `86ece81f-33bf-4230-9c5b-be55ea67855e`.

## Canonical state

Final `GET /operator-status` c114: investigation remains `running`; working-state v61, SHA `ec20349e4309b8bcf1138c87cc177bef9623fb3431b648751d660c273d5fbe2d`; no active jobs, ambiguous invocations, or canonical reconciliation items. G2 v119 approved and not revoked; VT, Bridge, Shodan allowed; acquisition allowed; TLP clear; `external_llm_allowed=true`; `do_not_submit=false`. Findings read again at v61 (context `a3215fb3…cb084`, page `ca42d447…3b0ad`); opinion read again at v61 (same context, page `82e804e…9c952`).

## Findings

- H1, claim S4P4.1 — `d5afc178-6357-59bd-a534-6d54af68162c`, `qualified`. A successful passive VT relation query returned 8 historical DNS resolutions for `91.107.156.29`, including `ns1.soroshpasargad.com` on 2025-08-29 (observations `17adef05-cff3-5d7a-b975-7a696e676b8e`, `16839908-102f-5d4d-8b71-600ffd2b6a14`). This supports historic co-association; it does not establish purchase, seller, contract, ownership, or compromised-host contact.
- H2, claim S2P2.1 — `2ccc6879-01c4-5504-baed-b4b6c2145594`, `qualified`. The article is the source for `RuntimeBroker.exe.config`/`etwEnable=false`. A VT-named `RuntimeBroker.dll` candidate received partial static analysis; a separate 524-byte `Client/Blackwood.dll.conf` object produced encoded `strings` output with no target marker recovered. Neither directly confirms the cited config or measures host ETW effects. No sample execution or host telemetry measurement occurred.
- H3, claim S6P1.1 — `4907bd4c-15b2-5aa5-b2ae-baac800068c5`, `qualified`. VT reported a sample as `Blackwood.dll`; partial CAPA/static analysis described sample-level features but did not qualify cluster membership. No independent link to Blackwood APT or another organization was established. Unit 42 remains the source for Iran-aligned attribution; cited group overlaps remain low-confidence.

## VT, acquisition, and local analysis

- 15 distinct documented SHA-256 file-report queries succeeded. The 91.107.156.29 historical resolutions query succeeded with complete pagination. Two MD5 report attempts returned `external_effect_refused` before provider routes/pages; they are not negative evidence. An earlier needs_review resolution request has equivalent later successful coverage; the earlier file-report invocation itself remains without archived output.
- Three `vt.download_sample` calls succeeded for the documented RuntimeBroker.dll (33,280 bytes), Client/Blackwood.dll.conf (524 bytes), and Blackwood.dll (8,487,424 bytes). All reused existing samples and remained candidates; canonical download budget consumption is zero. No sample was executed.
- RuntimeBroker.dll static analysis was partial: CAPA/FLOSS/static succeeded; SMDA unavailable (`unsupported architecture`); strings truncated; no goodware baseline. Blackwood.dll static analysis was partial: CAPA 9.4.0 reported 23 capabilities, FLOSS/static succeeded with truncated output, SMDA/code_features unavailable, no baseline; maximum executable-section entropy was 7.9998. Both analysis records explicitly leave samples as candidates and do not qualify campaign membership.
- Local `strings` on the .conf completed, but its output was encoded/binary-like; absence of target markers in that output is not evidence that the source bytes lack them.
- `samples.compare` succeeded on the two acquired DLL hashes and `features.common` reused the same `family_feature_comparison` artifact (SHA `1ee0519cb3c3b22235b9d9aafeff047c0857ea126bebeae4eebfef7da21cea8b`, 8,958,166 bytes). The observation endpoint returned 415 for that artifact; `evidence.get` exposed only its metadata and limitation that trait comparison does not confirm relatedness. No similarity score or negative conclusion is available.

## Opinion and Bridge

Draft opinion `ef1838cb-000f-53a1-bc0e-82dde8eb1179` is v3, SHA `4af95801ce005ce87c1bae4e2c19475259a4bef3da3327c528e010ff8d033f0e`, still `draft`; two blocks (`claim_verification`, `uncertainties_and_next_leads`) each reference the three current findings and 13 compact evidence refs. It has not been approved or published.

Bridge review remains unavailable; no review text was received. c100 and c111 ended needs_review with `bridge_server_error` at pre_submission and `confirmed_non_execution=true`; c113 single-unit attempt ended failed with `conversation_unavailable`, `retry=false`, `prior_submission_made=false`, zero Bridge turns consumed and zero response bytes. Its prompt/dossier preflight was READY, but no units were sent. The c100 observation was too large for brute-force reading; `advisor.reparse` c105 failed generically. The canonical status still shows two Bridge reservations (2 turns/7,200 tool seconds/context bytes and tokens reserved); c113's reservation was released. The canonical reconciliation list is empty despite those two needs_review records.

## Final budgets and remaining work

Canonical c114 budget (consumed / reserved / remaining): Bridge 1 / 2 / 197 turns; VT 30 / 0 / 9,970 units; downloads 0 / 0 / 1,000; Shodan 0 / 0 / 1,000; tool seconds 380 / 7,200 / 78,820; context tokens 9,151 / 2,941,079 / 13,049,770; context bytes 36,604 / 11,764,313 / 55,307,947.

The unresolved items are commercial provenance/ownership of the relay, the exact RuntimeBroker.exe.config and host-level ETW effect, independent actor/group attribution, readable detailed comparison metrics, and a Bridge review once the Bridge conversation transport is available. Investigation remains running. No approval or publication was made.
