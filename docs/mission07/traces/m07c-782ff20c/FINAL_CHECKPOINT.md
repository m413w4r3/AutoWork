# FINAL_CHECKPOINT.md

## Investigation

- Investigation: `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`
- Operator: `CTI-OPERATOR-A`
- Final canonical state: version **113**, SHA-256 `7fff517f192405b10e92fc3190bd25510f15dd603aea7114956ef3a8d8ded200`
- G2: active, not revoked; TLP Clear; `external_llm_allowed=true`.
- No opinion is currently composed. No sample was executed by the operator.

The static investigation progressed through the available 0–9 chain. Step 9 remains unresolved because Bridge returned `needs_review` without a usable draft, so the authorized dynamic complement was not submitted.

## Steps 0–9

| Step | Status | Result |
|---|---|---|
| 0 — Acquire and VT reports | Complete for the 15 documented samples | All 15 hashes had positive VT reports. All 15 download attempts produced candidate associations; 8 were newly acquired and 7 reused. Every association remains `candidate/not_confirmed`. |
| 1 — Article claims and Bridge plan | Partial | The untrusted article claims were enumerated and a Bridge investigation plan was received, but its normalized response was partial/unusable. The suggested managed-code review was pursued statically. |
| 2 | Not defined | The supplied plan has no step 2. |
| 3 — Static analysis | Partial | Candidate #3 is a managed PE/CLI assembly with a complete method inventory containing `PsProxy.PSExecutor.Execute(string command)`; the method body was unavailable. Candidate #5 has partial CAPA/.NET results, truncated or renamed method/string views, and no recoverable CIL. Candidate #7’s filtered ZIP listing contains a member with the same file hash as XML candidate #12. The project-content observation could not be retrieved by the canonical observation route (GET 404); archive extraction did not succeed. No sample was executed. |
| 4 — Protocol and crypto | No direct protocol evidence | VT sandbox metadata was available. PCAP retrieval is `needs_review` with no capture. Candidate #5 had an empty contacted-domains relation and one contacted-IP relation in VT. No packet protocol, message structure, key, IV, or derivation was recovered; no figure was rendered. |
| 5 — YARA draft | Unusable | The first Bridge request failed preflight because the project-content observation was absent from its dossier. A corrected PE-only request was submitted, but its archived response had no recoverable rule source, motifs, or invariants; local reparse returned `unsupported_legacy_format`. |
| 6 — Rule validation | Unavailable | `rule.test` is not exposed and the rule workbench is disabled. No rule was compiled or validated. |
| 7 — VT pivots | Partial, hash pivots complete | All 15 exact SHA intelligence searches completed. The two contacted-domain/IP relations for each of the 15 documented samples completed, including pagination for candidates #6 and #15; 22 relation calls returned results and 8 were empty. One additional MD5 report and the first IPv4-resolution request were refused before provider access; the remaining same-family MD5/resolution pivots were not retried. |
| 8 — Candidate downloads and revalidation | Partial | All 15 documented candidates were downloaded or matched to existing samples. No additional candidate was acquired from a new pivot. Revalidation with YARA was unavailable because no usable rule or test workbench exists. |
| 9 — Analyst opinion by Bridge and Bridge review | **Needs review; incomplete** | Bridge draft request `a228` ended `needs_review` with `model_submission_reconciliation_required`. All six units are unprocessed, no response text or output SHA is available, and `confirmed_non_execution=false`. No opinion was composed or reviewed. The reserved Bridge turn remains held; do not resubmit this dossier. |

## Canonical findings

- `7ec78429-7c0b-5fb5-b1cb-227cae1ede36` — `infection_chain`, **inconclusive**. Its observations establish the exact archive-member-to-file identity for candidates #7 and #12. The independent evidence-gate review supports that atomic byte-identity fact, but the versioned finding remains inconclusive; it was not revised during the read-only gate. Compilation, launch, and infection-chain execution are not established.
- `941dee1b-0688-513b-ab5f-baff9d094916` — `capability`, **qualified**. Candidate #3’s complete method listing records a command-taking method signature. No method body, in-memory PowerShell behavior, or `System.Management.Automation` use was demonstrated.
- `14fd5a52-2d51-5539-bb08-d382a2f90688` — `capability`, **qualified**. Candidate #5’s partial static analysis reported .NET and other capabilities; use at runtime and the article’s “RAT loader” label remain unverified.

## Budgets and unresolved invocations

At the final status read, the counters were:

- Bridge: **3 consumed, 1 reserved** of 200; provider usage unknown.
- VirusTotal: **72 consumed, 0 reserved** of 10,000.
- Downloads: **8 consumed, 0 reserved** of 1,000.
- Shodan: **0 consumed** of 1,000.
- Tool time: **745 seconds consumed, 3,600 reserved**.
- Context: **186,627 tokens and 746,502 bytes consumed; 544,706 tokens and 2,178,822 bytes reserved**.

The status endpoint’s ambiguity arrays were empty, but invocation `a228` itself remains `needs_review` with a reservation and no confirmation of non-execution. Treat that invocation as unresolved. No other invocation was active when the final status was read.

## Frictions and limits

- The separate crosswalk worker’s four exact section GETs returned `403 operator_client_scope_denied`; its numbered rows are provisional and were not used as verified ranks. An auto-review rejected a follow-up attempt to have another worker act under `CTI-OPERATOR-A`; no impersonation or workaround was attempted.
- The Bridge YARA artifact was not recoverable after local reparse. `rule.test` is unavailable.
- The PCAP request and the two refused VT branches were not repeated. No raw IOC or sample hash is copied into this checkpoint.
- The canonical body for the project-content observation returned GET 404. Its recorded command text is not used as a finding or as Bridge evidence.
- The last observation ref attached to `a228` is a 6,584,838-character untrusted payload; the invocation still has no Bridge output SHA or usable response text. It was not treated as an answer or reparsed.
- No software tests were run. No dynamic request file was submitted, because the brief requires static steps 0–9 to finish first.

## Next permitted action

Reconcile the exact `a228` invocation and its held reservation; do not resend it while submission remains unresolved. If a complete Bridge response becomes available, reparse that response locally and compose only a Bridge-authored draft. Request Bridge review against the exact draft version and SHA. If non-execution is confirmed and the reservation is cleared, a fresh, reduced Bridge draft request may be considered under the existing authorization. After static steps 0–9 are closed, submit the authorized QEMU/KVM dynamic requests for the orchestrator to run; the operator must not execute samples.
