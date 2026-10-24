# R7 — Independent review of the provisional advisor hybrid

**Verdict: REVISE**

**Review basis:** source at `aa0cef3ee07525ca77d6b1358252ebf5735a3dde` (`HEAD` matched this SHA). The selected interface is a plausible deep module if it truly owns preparation, ref identity, and semantic parse dispatch. The current proposal does not yet specify enough to guarantee exact prompt/ref binding, safe caller use, or historical reparse parity. These are design gaps, not verified defects in a new implementation.

Labels: **VERIFIED** means present in the cited source or artifact; **INFERENCE** means a failure scenario implied by the proposed interface; **UNKNOWN** means not established by source review or tests. No tests were run.

## Ranked findings

### HIGH — A single alias/ref ledger is not yet collision-safe

**Evidence — VERIFIED:** the selected hybrid requires `(kind, id, sha256)` identity and fail-closed collisions (`DESIGN_OPTIONS.md:306`, `D6_ADVISOR_A_MINIMAL.md:166-180`). Current ref aggregation keys on `(kind, id)` and overwrites entries in `_typed_transmitted_refs` (`investigation_advisor_tools.py:4453-4466`); dossier filtering also compares only `(kind, id)` (`investigation_advisor_tools.py:4630-4648`). `advisor_wire_aliases.add` uses the same pair and only fills a missing digest; a conflicting non-null digest is retained silently (`advisor_wire.py:94-106`). When a manifest exists, aliases are built from both `transmitted_refs` and manifest entries (`advisor_wire.py:153-177`); the existing ambiguity check catches a repeated alias with a different triple, not two digests for one `(kind, id)` (`advisor_wire.py:229-257`).

**Failure scenario — INFERENCE:** the filtered projection and manifest carry the same source ID at different versions. The `R` catalog can retain one SHA while an `E` manifest alias carries the other; persistence and parsed citations can then describe different source versions. A manifest entry can also become an alias even if it is absent from the transmitted-ref set.

**Guarantee not assured:** every response alias resolves to one exact, transmitted version, and two source rows sharing `(kind, id)` cannot silently collapse.

**Required oracle:** inject same `(kind, id)` with different SHAs across the projection, manifest and transmitted refs; preparation must refuse. Also assert aliases for absent, restricted, stale, and wrong-kind refs cannot resolve. Compare each resolved triple with the bytes and refs archived for that dispatch.

**Possible design fix:** build one immutable catalog keyed by exact triples, enforce `(kind, id) -> exactly one SHA`, and require every response alias target to be in the prepared transmitted-ref allowlist (with an explicit typed exception only for versioned review targets). Do not recover a ref by ID alone.

### HIGH — The prepared bytes are not mechanically bound to archive and S3

**Evidence — VERIFIED:** the hybrid promises exact UTF-8 bytes and a SHA (`DESIGN_OPTIONS.md:257-263`). Today the caller hashes `encoded`, archives `prompt` through `put_text`, and submits the separate string `prompt` with `context_sha256` (`investigation_advisor_tools.py:741-778`; `production_artifact_store.py:50-53`). `submit_bridge_consultation_once` receives both `context_sha256` and `prompt` as independent values (`bridge_investigator.py:248-252`).

**Failure scenario — INFERENCE:** a caller decodes and re-renders or normalizes a prepared prompt, then archives/submits the changed string while preserving the prepared hash or refs. Strict UTF-8 decode/encode alone is reversible, but the interface does not require the caller to use that exact round trip.

**Guarantee not assured:** `PreparedConsultation.prompt_utf8`, archived prompt bytes, transport input bytes and S3 idempotency context are identical.

**Required oracle:** through the real advisor caller with fake archive and transport, assert byte equality among prepared bytes, archived bytes and `transport_prompt.encode("utf-8")`; assert the stored/S3 context hash equals SHA-256 of those same bytes. Include Unicode and CRLF input.

**Possible design fix:** make prepared bytes the only output of rendering; archive those bytes directly; strictly decode once only at the existing string-based S3 seam; verify the hash at that adapter. Do not pass prompt data back to callers for a second render.

### HIGH — “Already filtered projection” is a caller convention, not a guard

**Evidence — VERIFIED:** current `execute` performs sample filtering and blocks explicit restricted refs before the case disclosure check and preparation (`investigation_advisor_tools.py:323-376`); external policy is checked at `:362-376`. The transport seam takes an `external_llm_allowed` boolean and trusts the caller’s value (`bridge_investigator.py:261-282`; M4 diagnosis, `M4_ADVISOR_DIAGNOSIS.md:27-29`). The design says `prepare` has no policy or transport responsibility (`DESIGN_OPTIONS.md:311-312`). A designer calls `AuthorizedProjection` a statement about call order, not a token (`D6_ADVISOR_A_MINIMAL.md:116-120`).

**Failure scenario — INFERENCE:** a new caller passes an unfiltered projection to `prepare`, then submits the result with `external_llm_allowed=True`. The module itself has no evidence that sample restrictions, TLP, `do_not_submit`, revocation or case disclosure checks ran. Preparation alone does not send, but its result can carry restricted content to S3 if the caller skips those checks.

**Guarantee not assured:** “prepared” means policy-cleared, or that sample policy is rechecked by S3. It does not. `external_llm_allowed` must not be accepted as an input to `prepare` or treated as sufficient sample authorization.

**Required oracle:** through the actual `InvestigationAdvisorTool.execute` path, independently deny external disclosure, `do_not_submit`, TLP RED and a restricted explicit sample; assert preparation cannot produce a dispatchable result and fake S3 receives zero calls. For the allowed case, assert permitted CTI survives. The module-only test is insufficient.

**Possible design fix:** keep authorization in the existing caller; expose only an immutable post-filter projection type to this internal seam, with no authorization boolean, and keep `prepare` free of policy claims. Add a caller-level policy matrix proving the ordering. Do not treat a nominal type as a security token.

### HIGH — Stored parse context and parse outcomes need a fail-closed contract

**Evidence — VERIFIED:** live parsing has typed v3 and legacy v2/wire paths (`investigation_advisor_tools.py:4745-4791`, `:5067-5315`); current reparse accepts only `yara_draft`, `draft_text`, `review_text`, and `figure_plan` (`investigation_advisor_tools.py:3442-3487`), verifies stored observation/output hashes and provenance (`:3455-3555`), checks stored refs/context or uses a legacy converter (`:3557-3628`), then invokes the typed parser only (`:3629-3637`). It does not call Bridge or reload evidence in the shown flow. Ambiguous/unsuccessful submissions are gated before normal parsing (`investigation_advisor_tools.py:784-789`, `:2979-3089`). Suricata context is captured but its type is absent from the current reparse allowlist (D6 brief, `D6_BRIEF_ADVISOR.md:53-60`).

**Failure scenario — INFERENCE:** a stored v2 response lacks its prompt-era aliases or schema/context version. A new parser guesses from current data, relabels it as v3, or silently returns a warning/partial result. A separate failure is expanding reparse to Suricata without the exact stored network-fact context.

**Guarantee not assured:** every historical v2/v3 record can be interpreted faithfully. The proposal has not defined a versioned `StoredParseBasis`, missing-context outcome, or whether old v2 plan/pivot records are newly eligible for reparse.

**Required oracle:** freeze historical v2 JSON/wire and v3 JSON/wire records with expected outcomes and exact resolved refs. Test absent and malformed context returns non-usable `legacy_context_unavailable`, with zero evidence reload and zero Bridge calls. Keep `suricata_draft` gated unless stored network facts, refs and hashes are complete and verified. Do not expand plan/pivot reparse as a side effect.

**Possible design fix:** persist a parse-context version separately from `response_schema` and `parser_version`; provide an explicit legacy converter; make missing context an explicit invalid/non-usable outcome. The reparse adapter must continue verifying the observation, blob descriptor, bytes and output SHA before constructing a stored basis.

### MEDIUM — `unit_plan` could change dispatch and budget semantics

**Evidence — VERIFIED:** the provisional hybrid adds `unit_plan` (`DESIGN_OPTIONS.md:302-305`), while current editorial batching owns chunking, turn admission, child idempotency keys, cursor and partial/unprocessed outcomes in the caller (`investigation_advisor_tools.py:1203-1244`, `:1253-1383`). Each child dispatch receives its own prepared prompt context; the existing S3 call includes that context hash (`investigation_advisor_tools.py:765-778`).

**Failure scenario — INFERENCE:** the module interprets `unit_plan` as an execution plan and changes chunk count or unit grouping. That can multiply Bridge turns, exceed the reservation, or alter which units are partial/refused/unprocessed.

**Guarantee not assured:** adding the module leaves editorial dispatch count, budget consumption, ambiguity recovery and per-unit refusal semantics unchanged.

**Required oracle:** freeze a multi-unit case and compare old/new chunk membership, number of submissions, unit outcomes, cursor and consumed turns; assert each child identity is bound to its exact prompt hash and unit IDs. Exercise one refused child and confirm later units remain explicitly unprocessed.

**Possible design fix:** keep chunking and S3 calls in the existing caller. If retained, `unit_plan` must describe only the already-selected single dispatch; it must not split, submit, aggregate or consume budget.

### MEDIUM — Preservation and omission observability are underspecified

**Evidence — VERIFIED:** AutoWork requires permitted CTI evidence to remain permissive and every omission to have an observable reason (`backend/AGENTS.md:55-70`; `D6_BRIEF_ADVISOR.md:84-101`). The dossier is a relevance-selected projection; the audit notes generic field omissions need not each appear in its manifest (`M1B_ADVISOR_FLOW.md:77-84`). The hybrid lists a dossier manifest but does not say that all projection omissions or redactions are represented there (`DESIGN_OPTIONS.md:257-312`).

**Failure scenario — INFERENCE:** a refactor replaces current dossier/preflight logic with a narrower prepared payload, dropping raw/hex/Base64/network bytes, contradictory records or permitted attacker tokens, or redacts CTI-looking material without recording why.

**Guarantee not assured:** the module introduces no new evidence cap, omission, over-redaction or silent truncation. A selected-item manifest is not automatically an omission audit.

**Required oracle:** golden authorized input containing raw bytes, hex, Base64, network fields, long excerpts, contradictions and generic attacker `api_key`/token values. Assert exact included bytes, source SHA/offset/length and manifest; add negative operational-secret/refusal cases and assert their reasons. Keep the existing completeness refusal semantics.

**Possible design fix:** reuse `build_advisor_dossier`, the secret guard and completeness preflight rather than reimplementing them; carry existing omission/completeness results in the prepared/refusal result. No new fixed cap or broad field-name filter.

### HIGH — The migration gate does not yet prove the new seam is used

**Evidence — VERIFIED:** C proposes additive expand/contract, tests-only dual execution without a second send or persisted shadow parse, and rollback by caller wiring while retaining old fields (`D6_ADVISOR_C_MIGRATION.md:158-181`). Existing tests exercise the current caller/parser and fake adapters, not a future module: examples include `test_minimal_plan_assembles_evidence_and_parses_structured_output` (`test_investigation_advisor_tools.py:1186-1218`), `_parse_v2` directly calling `_parse_advisor_output` (`test_advisor_wire.py:479-489`), and direct reparse against the current implementation (`test_advisor_output_reparse.py:156-178`). The audit also labels these as fake/in-memory seams (`M2B_TEST_SURFACE_ADVISOR_AUDIT_PUB.md:23-28`).

**Failure scenario — INFERENCE:** the new module is added as a forwarding wrapper while consultation, reparse and tests keep using the old functions. All existing tests can remain green. Or, a “dual path” runtime implementation sends twice or persists a shadow parse despite the migration text intending fixture-only comparison.

**Guarantee not assured:** the module owns the new caller path, rollback can read records produced during cutover, and the deletion test removes old decision logic instead of only its wrapper.

**Required oracle:** before cutover, run old and new code on frozen local fixtures only; assert no transport call, no observation append and no shadow parse during comparison. Then add caller-level tests that spy on the new `prepare`/`parse` methods, prove exact archive/S3 coupling, and exercise stored reparse through the same parser. The deletion gate must remove or make unreachable old preparation aggregation and `_parse_advisor_output`/legacy semantic dispatch from both callers.

**Possible design fix:** retain additive context fields and the old `reparse_context` shape until rollback expires; make observation persistence derive refs/hash/schema from the prepared value, not by rebuilding them. Keep `AnalystOpinionService` unchanged. Rollback is conditionally credible only if old code can read new records and no immutable bytes/observations are rewritten.

## Explicit answers to the 11 attack scenarios

1. **Unfiltered projection / `external_llm_allowed`:** yes, a caller can pass unfiltered data because `prepare(projection)` is pure and no policy evidence is specified. That does not itself authorize or send, but the existing S3 seam trusts its caller’s boolean and does not enforce sample filtering. Keep the boolean out of `prepare`; gate the actual caller path and test it.
2. **Exact bytes/hash:** not proven by the interface alone. Current code hashes encoded bytes but archives and submits a separate string. Require direct byte equality and hash equality at the actual caller/S3 seam.
3. **Ref identity:** not assured. Current catalogs collapse by `(kind,id)` and can retain conflicting manifest/transmitted SHAs. Require fail-closed triple identity and an alias-subset oracle.
4. **Refusal/partial/invalid/ambiguous/unsuccessful:** main typed and legacy advisor parsers preserve explicit `refused` (`advisor_contracts.py:485-503`; `investigation_advisor_tools.py:5264-5275`); the live caller maps refusal to partial status (`:3140-3158`). The new `ParseOutcome` must encode refusal, partial and invalid explicitly. Ambiguous/unsuccessful are transport outcomes and must never be passed to `parse` (`:784-789`, `:2979-3089`). The P10 warning-only refusal defect is a sibling path identified in the brief (`D6_BRIEF_ADVISOR.md:57-60`); do not route P10 through this module.
5. **Reparse:** verified stored v2 contexts can only be supported where their stored schema/context and aliases are sufficient; missing context must fail non-usable without guessing or evidence reload. Suricata stays gated. The adapter retains blob/output verification; the parser module gets stored context and bytes only.
6. **Editorial batching:** `unit_plan` must not change dispatch count, budget or ambiguity behavior. The caller keeps batching and one prepared value must bind to one actual child S3 submission, including prompt hash and unit identity.
7. **Truncation/redaction/omission:** no new cap or broad redaction is acceptable. Preserve raw/hex/Base64/network bytes, offsets, lengths, contradictions and permitted CTI-like tokens; make required omissions and secret refusals observable.
8. **Opinion provenance:** the proposal need not weaken it if `AnalystOpinionService` remains independent. It still verifies persisted advisor-call blobs and exact draft/review hashes (`analyst_opinion_service.py:917-1008`; M1 flow, `M1B_ADVISOR_FLOW.md:30`). Any parser `provenance_eligible` flag is advisory and cannot replace that check or the opinion version/CAS path.
9. **Migration/rollback:** the proposed additive expand/contract is acceptable conditionally. Fixture dual-run must have no send or persisted shadow parse. Rollback is real only while old fields remain readable and old code passes against new records; no blobs or immutable observations are rewritten.
10. **Deletion test:** remove tool-local ref aggregation, prompt/preparation duplication and old semantic dispatch only after the replacement callers cross the new interface. Keep dossier selection, secret guard, S3 submission, stored-byte verification, observation persistence and opinion validation in their existing owners. A wrapper around `advisor_dossier`, `advisor_wire_aliases` and old parser functions fails the deletion test.
11. **Tests that can stay green / missing first-lot oracle:** current pure dossier/wire tests, fake-transport `execute` tests, direct old-parser tests and direct reparse tests can all stay green while the new module is unused. Gate the first implementation lot on tests that assert the real consultation and reparse callers invoke the new interface, exact prepared/archive/S3 byte and ref equality, policy-denial zero-send behavior, stored-context fail-closed behavior, and unchanged opinion provenance. The existing focused tests do not by themselves prove module adoption.

## What is approved, conditional, and not ready

- **Approved direction:** a pure application `advisor_consultation` module with exactly `prepare` and `parse`; S3 remains the only send seam; canonical evidence, authorization, budgets, consumption, raw-output archival and opinion versioning remain outside it. No new persistent source of truth is needed.
- **Conditional:** the selected `(kind,id,sha256)` catalog; parser consolidation across v2/v3; additive parse-context field; test-only old/new comparison; rollback-by-wiring. Each requires the collision, golden-byte, historical parse and no-shadow-parse oracles above.
- **Do not implement yet:** production cutover or deletion of old parser branches before the frozen v2/v3 corpus passes; any automatic Suricata reparse expansion; any change that lets `unit_plan` drive dispatch; any opinion-service bypass; any P10 routing; any second send or runtime shadow parse.

## Tests and commands

- **Tests run:** none. No pytest, provider, PostgreSQL, MinIO, Docker or CTI network call was made.
- **Repository check:** `git rev-parse HEAD` returned `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`.
- **Source inspection:** targeted `codegraph explore` queries for the named advisor, reparse, batching, S3 and opinion symbols; targeted `rg -n` for focused test names; line-numbered reads of only the requested design sources plus their cited source/tests.

