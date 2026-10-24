# R7-ADV-R2 — Independent adversarial review of the revised advisor hybrid

**Verdict: REVISE** for caller cutover. The pure preparation slice below is safe to start; do not wire it to S3 or change reparse yet.

**Review basis:** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`, branch `mission03/architecture-design`; `HEAD` matched. Read the R2 section and named advisor artifacts, then inspected the cited code through CodeGraph. No S3 designer/reviewer artifacts were read.

**Labels:** VERIFIED = directly present in source or artifact; INFERENCE = consequence of those facts; UNKNOWN = not proven here. This is a design review, not implementation validation.

## Closure of the first review

| First-review finding | Status | R2 closure and remaining condition |
|---|---|---|
| HIGH — collision-safe ref ledger | **CLOSED** | R2 refuses differing SHAs for one `(kind,id)` across projection, manifest and transmitted refs; aliases must target transmitted refs; there is no ID-only resolution (`DESIGN_OPTIONS.md:410-416`). The versioned review-target exception is necessary: current targets carry a versioned `source_ref` with opinion version, SHA and block type (`advisor_dossier.py:406-434`), and wire aliases normalize the opinion target to that `source_ref` (`advisor_wire.py:236-249`). Implement that exception against the exact versioned `source_ref` triple, never the stable opinion UUID alone. |
| HIGH — prepared/archive/S3 byte binding | **CLOSED, with an adapter oracle required** | Bytes-only prepared output, direct byte archival, one strict decode and SHA equality are explicit (`DESIGN_OPTIONS.md:417-421`). The archive API accepts bytes (`production_artifact_store.py:50-59`); the Bridge helper accepts a string (`bridge_investigator.py:248-262`). The caller must check the string re-encoded as UTF-8 immediately before calling the helper. The actual model-conversation service strips the message before storing/hash calculation (`model_conversations.py:1101,1189-1195`), so verify equality after that stage too. Current `_prompt` output starts with `FORMAT:` and ends with a nonblank contract line (`investigation_advisor_tools.py:2855-2947`), making the strip a no-op for the current renderer; lock that property in an offline oracle. |
| HIGH — authorization boundary | **PARTIAL** | R2 correctly excludes policy booleans/capabilities from `prepare`, rejects treating a nominal type as authority, and requires the real caller policy matrix to assert zero S3 calls (`DESIGN_OPTIONS.md:422-428`). But R2 also says S3 consume rechecks the current envelope. At this SHA, `submit_bridge_consultation_once` accepts only `external_llm_allowed: bool` and checks that value (`bridge_investigator.py:248-282`); `ModelConversationTurnTransport.submit` does the same (`:642-667`), and `ModelConversationService.add_turn` receives that bool and copies it into `ModelRequest` (`model_conversations.py:1086-1100,1267-1281`). No current-envelope lookup/recheck is present in those seams. The caller matrix proves the listed denials at entry, not a policy change between that check and consume. |
| HIGH — stored parse context and outcome contract | **PARTIAL** | R2 specifies a distinct context version, fail-closed `legacy_context_unavailable`, no evidence reload/Bridge, Suricata gating and distinct refusal/partial/invalid/ambiguous outcomes (`DESIGN_OPTIONS.md:429-438`). It does not specify how existing `operator-advisor-call.v1` records, which predate `parse_context_version`, map to a supported legacy context version. Existing reparse uses `reparse_context` when present and a legacy converter when absent (`investigation_advisor_tools.py:3592-3628`). Without a named converter/admission rule and frozen expected v2/v3 outcomes, the new parser either makes all old records unavailable or guesses. A single version marker on the existing `reparse_context` is sufficient; do not introduce a second context payload/source of truth. |
| MEDIUM — `unit_plan` dispatch/budget semantics | **CLOSED** | R2 removes it or makes it read-only for one already-selected dispatch; batching, child IDs, cursor, budget and per-unit outcomes remain caller-owned (`DESIGN_OPTIONS.md:439-442`). |
| MEDIUM — omission/redaction observability | **CLOSED** | R2 reuses the dossier, secret guard and completeness preflight and carries their omission/completeness results in prepared/refusal outcomes, with no new caps or broad redaction (`DESIGN_OPTIONS.md:443-446`). This matches the first review’s proposed fix. Keep the existing dossier and completeness behavior rather than treating the selected-item manifest as a complete omission log. |
| HIGH — migration/adoption gate | **PARTIAL** | R2 requires fixture-only old/new execution with no send, append or shadow parse; caller-level tests proving the new methods are used; and a deletion test removing old aggregation/semantic dispatch (`DESIGN_OPTIONS.md:447-454`). Specify that the fixtures are a frozen v2 JSON/wire and v3 JSON/wire corpus with expected statuses and exact refs before parse/reparse cutover. “Local fixtures” alone does not establish historical parity. |

## Remaining and newly exposed findings

### HIGH — R2 assumes a current-policy consume check that the existing S3 path cannot perform

**VERIFIED:** the advisor caller checks case disclosure and sample restrictions before preparation (`investigation_advisor_tools.py:323-376`). The S3 helper and transport then receive a boolean; neither has the current envelope needed to re-evaluate revocation or updated policy (`bridge_investigator.py:248-282,642-667`; `model_conversations.py:1086-1100,1267-1281`). M4 also describes the transport as trusting its caller’s boolean (`M4_ADVISOR_DIAGNOSIS.md:27`).

**INFERENCE:** a caller-level matrix can correctly prove that `external=false`, DNT, RED and restricted explicit refs yield zero calls on the tested path, but it cannot prove the R2 claim that consume rechecks current policy. Keep preparation unprivileged; before caller cutover, either give the consume boundary a current policy authority/check and test revocation between caller check and consume, or revise R2 so it no longer claims that check and explicitly accepts the current boundary.

### HIGH — historical parse-context version selection is undefined

**VERIFIED:** current stored observations use `operator-advisor-call.v1` with `reparse_context`; they do not carry the proposed `parse_context_version` (`M1B_ADVISOR_FLOW.md:58-60`; current writer references in `investigation_advisor_tools.py:901-1071`). Current reparse has an explicit legacy reconstruction path (`:3592-3628`).

**INFERENCE:** every existing record lacks the new marker. R2 must say which complete legacy shapes are converted to which context version, and which fail closed. Its new marker can version the existing context payload without adding another truth source. Require frozen v2/v3 JSON and wire fixtures through live parse and stored reparse, including missing/malformed context, before parser cutover. The fail-closed result is safe, but blanket loss of historical reparse must be an explicit compatibility decision.

### MEDIUM — byte oracle must observe the input after the string adapter

**VERIFIED:** `ProductionArtifactStore.put_bytes` archives exact bytes, but `ModelConversationService.add_turn` strips its string before UTF-8 encoding and storing it (`production_artifact_store.py:50-59`; `model_conversations.py:1101,1189-1195`). Current `_prompt` has nonblank boundaries, so source inspection suggests stripping is currently a no-op (`investigation_advisor_tools.py:2855-2947`).

**INFERENCE:** a fake attached only to `submit_bridge_consultation_once` can prove what that helper received, not what the model-conversation seam stores. Include Unicode and CRLF inside the rendered prompt and compare prepared bytes, archive bytes, and bytes observed after the downstream normalization. No provider call is needed.

## Smallest safe first implementation slice

Implement only the pure `prepare` path and its immutable ref catalog. Reuse `build_advisor_dossier`, the existing secret guard and completeness preflight. Return exact `prompt_utf8`, its digest, transmitted refs and alias catalog. Do not wire it into `InvestigationAdvisorTool.execute`, archive/send, persist a new context field, change reparse/parser dispatch, or alter opinion code in this slice. This keeps the change reversible and creates no new egress or historical-record behavior while the two HIGH gaps above are resolved.

**First RED oracle:** a pure preparation test with the same `(kind,id)` and different valid SHAs introduced separately through projection, manifest and transmitted refs must return `ref_reference_conflict`; aliases for absent, restricted, stale and wrong-kind refs must not resolve. Add the explicit versioned review-target fixture and require its alias to resolve to the exact `source_ref` version/SHA. Also assert that the returned prompt bytes/hash are deterministic for Unicode and embedded CRLF. This oracle should fail before the catalog/prepare implementation is added.

Before any caller cutover, conditions that must remain true:

- The real `InvestigationAdvisorTool.execute` matrix independently denies external=false, DNT, RED and restricted explicit sample refs and proves the fake S3 seam receives zero calls; allowed CTI markers retain their manifest identity.
- The S3 consume policy check is implemented or R2 is corrected to describe the actual boolean trust boundary; no nominal prepared type grants authority.
- Prepared, archived and downstream model-input bytes and SHA match; one strict decode is used at the string seam.
- Stored context is a single versioned payload with an explicit legacy converter/fail-closed table; Suricata remains gated; no evidence reload or Bridge call occurs during reparse.
- A frozen v2/v3 corpus passes through live parse and reparse before cutover. Old prep/semantic dispatch is removed or unreachable from both callers after adoption; no runtime dual-send or shadow parse occurs.
- Batching stays in the caller; dossier, secret guard and completeness/omission results remain observable; no new cap or broad redaction is introduced.

## Validation record

- Tests run: **none**. No pytest was run.
- No provider, PostgreSQL, MinIO, Docker or CTI network was called.
- Read-only checks: `git rev-parse HEAD` → `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`; `git status --short --branch`; targeted CodeGraph queries for `InvestigationAdvisorTool.execute`, reference builders, parser/reparse, `submit_bridge_consultation_once`, `ModelConversationTurnTransport.submit`, `ModelConversationService.add_turn`, `_prompt`, `advisor_wire_aliases`, `ProductionArtifactStore.put_bytes`; line-numbered reads of the named R7, R2 and advisor artifacts.
- The repository was already on `mission03/architecture-design` with `docs/mission03-architecture/` untracked before this review. This artifact is the only file written by this review.
