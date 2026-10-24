# D6-ADV-C — Advisor seam: common caller and migration

**Design point:** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde` (`mission03/architecture-design`).
**Scope:** design only. No production, test, migration, or configuration files were changed; no tests or operational services were run.

Labels below distinguish **VERIFIED** source facts, **INFERENCE** design consequences, and **UNKNOWN** runtime or compatibility questions. The recommendations optimize the normal `InvestigationAdvisorTool.execute` consultation, while keeping `advisor.consult`, persisted-output reparse, and opinion provenance consumers compatible during an expand/contract migration.

## 1. Current seam and proposed seam

**VERIFIED — current seam.** `InvestigationAdvisorTool.execute` is the real consultation adapter. It validates the request, loads and filters evidence, checks disclosure, builds the prompt projection, redacts, builds the dossier and completeness report, renders and sizes the prompt, derives reference/alias state, archives the prompt, calls `submit_bridge_consultation_once`, archives the returned raw output, dispatches parsing, and maps transport/parse results into `ToolResult` (`backend/src/cti_app/application/investigation_advisor_tools.py:164-778, 2855-3180`). The reparse tool has a separate parse path at `:3414-3787`.

**INFERENCE — proposed seam.** Add one deep application module beside the existing modules, e.g. `advisor_consultation.py`, with two public operations:

1. `prepare(input) -> Prepared | PreparationRefusal`
2. `parse(raw_output, context) -> Parsed | ParseFailure`

The external seam sits after the caller has performed request admission, loaded canonical state, applied sample restrictions, and checked `external_llm_allowed`, but before prompt archival and the existing S3 submission helper. `prepare` owns the remaining advisor-specific projection into exact prompt bytes and the single reference/alias ledger. `parse` owns typed and legacy response interpretation using that ledger. Neither operation sends, authorizes, consumes budget, persists, or changes canonical state.

This puts substantial preparation and parsing behavior behind two small interface operations. The normal caller supplies its already-admitted projection, archives the returned prompt, calls the existing S3 seam once, archives raw output, and asks the same module to parse only a successful submission. The default flow is straightforward without making the new module an alternate transport or policy owner.

## 2. Public contract

Illustrative Python types; names are design names, not existing code:

```python
@dataclass(frozen=True)
class AdvisorPrepareInput:
    consultation_type: str
    prompt_data: Mapping[str, object]  # after case/sample disclosure filtering
    settings: AdvisorPreparationSettings

@dataclass(frozen=True)
class PreparedConsultation:
    prompt_bytes: bytes                 # exact UTF-8 bytes to submit
    prompt_sha256: str
    dossier_sha256: str
    dossier_manifest: tuple[Mapping[str, object], ...]
    transmitted_refs: tuple[AdvisorRef, ...]
    observation_refs: tuple[str, ...]
    source_refs: tuple[str, ...]
    alias_ledger: AdvisorAliasLedger
    reparse_context: Mapping[str, object]  # serializable, versioned snapshot
    parser_contract: str                 # existing response contract version

@dataclass(frozen=True)
class AdvisorParseContext:
    consultation_type: str
    transmitted_refs: tuple[AdvisorRef, ...]
    alias_ledger: AdvisorAliasLedger
    reparse_context: Mapping[str, object]
    parser_contract: str | None

def prepare(input: AdvisorPrepareInput) -> PreparedConsultation | PreparationRefusal: ...
def parse(raw_output: bytes, context: AdvisorParseContext) -> ParsedAdvisorOutput | ParseFailure: ...
```

`AdvisorRef` has the established `kind`, `id`, and `sha256` fields; where a source has offsets, length, or source identity, those remain in the manifest/context rather than being discarded. The alias ledger records the response-visible alias and the exact reference identity it resolves to. The parser context is a snapshot, not a lookup instruction: reparsing must not reload canonical evidence to guess what an old prompt contained.

**Interface invariants:** `prepare` is deterministic for identical inputs and returns bytes whose SHA-256 is `prompt_sha256`; it never truncates CTI evidence silently. `parse` has no I/O and is deterministic for identical raw bytes and context. Invalid UTF-8, invalid output, and explicit model refusal remain distinguishable outcomes. It must not reinterpret ambiguous or unsuccessful transport outcomes; callers invoke it only after a successful submission with archived raw bytes.

## 3. Invariants, timing, versions, errors, confidentiality

**VERIFIED.** Current flow checks the case's external disclosure permission before preparation (`investigation_advisor_tools.py:362-376`), then applies evidence/sample restrictions and operational-secret redaction before submission (`:324-435`). Dossier selection and completeness are in `advisor_dossier.py:304-939, 942-1687`; wire rendering and aliases are in `advisor_wire.py:90-273`. The exact prompt is archived before transport, and successful raw output is archived before parsing (`investigation_advisor_tools.py:730-886`).

**INFERENCE — preserve these contracts.**

- Keep the existing order: existing budget reservation/admission; request validation and canonical reads; policy and sample checks; advisor preparation; prompt archive; S3 submission; raw-output archive; parse; versioned observation. Do not move preparation ahead of existing admission or disclosure checks.
- Keep `advisor-wire.v1`, `advisor-consult.v1`, typed `advisor-response.v3` / `advisor-source-parser.v3`, and the legacy `advisor-response.v2` path. Preserve the current `review_text` typed-to-legacy fallback and the current behavior for other typed parse failures.
- Preserve statuses and `ToolResult` mapping: explicit parsed `refused` remains `advisor_status="refused"` and a partial invocation; partial normalization remains partial; invalid output remains failed; blocked stays policy denied; ambiguous/time-out states remain needs review and are never parsed. Preflight refusals remain preflight refusals with their existing codes and evidence references.
- Preserve exact prompt/output hashes, dossier manifest, reference IDs, offsets, lengths, aliases, source refs, turn ID, and invocation provenance. Prompt bytes used for the hash, archive, and S3 call must be identical.
- Preserve the current configured input-size/preflight outcome and editorial batching behavior. Do not add lower fixed caps or silently clip raw, hex, Base64, byte, network, IOC, long-excerpt, or contradictory CTI evidence. Only explicit disclosure restrictions and operational-secret controls remove/block data, with an observable reason.
- Preserve the current parser-specific Bridge/UI marker sanitization behavior. Raw output bytes and their SHA remain untouched in the archive; sanitizing a parser view must not rewrite the archived blob.
- Raw model output remains a non-canonical consultation artifact. It may be retained by SHA and referenced by `operator-advisor-call.v1`; parsing it does not promote it to canonical CTI evidence.
- The module must not log raw evidence, prompt text, parser excerpts, operational credentials, or signed URLs. Reparse context stays in the existing authorized immutable observation/blob path and is not sent to a provider.

**UNKNOWN.** Whether every production artifact adapter and every persisted historical `reparse_context` shape is represented by current offline fixtures is not established by source inspection.

## 4. Normal caller example

The most frequent normal flow in `InvestigationAdvisorTool.execute` should read conceptually as:

```python
# Existing code has already admitted the request, loaded canonical rows,
# applied sample restrictions, and checked external_llm_allowed.
prepared = self._advisor_consultation.prepare(
    AdvisorPrepareInput(consultation_type, filtered_prompt_data, settings)
)
if isinstance(prepared, PreparationRefusal):
    return await self._persist_existing_preflight_outcome(context, prepared)

await self._artifact_store.put_bytes(prepared.prompt_bytes, bucket="advisor-prompts")
submission = await submit_bridge_consultation_once(
    self._transport,
    investigation_id=context.investigation_id,
    subject_id=investigation.subject_id,
    turn_index=turn_index,
    context_sha256=prepared.prompt_sha256,
    prompt=prepared.prompt_bytes.decode("utf-8"),
    external_llm_allowed=True,  # result of the existing admission path
    settings=self._settings,
    timeout_seconds=execution_timeout_seconds,
    deadline_monotonic=execution_deadline_monotonic,
    minimum_timeout_seconds=minimum_execution_seconds,
)
# Existing outcome mapping rejects blocked/ambiguous/failed submissions.
# On success: archive the raw bytes first, then call parse(raw, prepared context).
```

This removes manual caller assembly of dossier hashes, transmitted refs, response aliases, parser prompt data, and reparse context. The caller still owns the authorization path, prompt/output persistence, S3 call, transport result/status mapping, observation append, and `ToolResult` construction.

## 5. Responsibilities hidden in the implementation

`prepare` concentrates: redaction/residual-secret refusal; dossier construction; completeness and contradiction checks; prompt rendering and current size/preflight decisions; exact UTF-8 serialization and hashes; transmitted-reference selection; stable alias-to-reference mapping; observation/source reference derivation; and the versioned reparse snapshot. Editorial batch splitting remains orchestration in `InvestigationAdvisorTool.execute`; preparation returns the same size refusal details that cause the existing adapter to split or refuse. It should call the existing `build_advisor_dossier` and secret guard rather than replace those KEEP modules.

`parse` concentrates: v3 typed response validation and refusal semantics; v2 JSON and legacy `advisor-wire.v1` compatibility; alias resolution against the prepared ledger; current parser-specific marker handling, safe text normalization, warning collection, and stable normalized response shape. The new module should own the semantic dispatch that is now split between `advisor_contracts.py`, `advisor_wire.py`, and private functions in `investigation_advisor_tools.py`.

These operations do not own canonical evidence reads, disclosure authority, budgets, editorial orchestration, S3/Bridge transport, raw-output blob writes, observation persistence, or opinion versioning.

## 6. Responsibilities removed from callers

After cutover, `InvestigationAdvisorTool.execute` no longer reconstructs aliases, independently derives transmitted refs, renders/serializes the prompt, or decides which semantic parser to call. It calls `prepare`, performs the existing archive and one S3 submission, then passes the archived successful response and prepared context to `parse`.

`ReparsePersistedAdvisorOutputTool.execute` no longer calls `parse_typed_advisor_response` itself or recreates parser behavior. It continues to validate the original observation, verify the output blob descriptor and SHA/size, load immutable output bytes, build an `AdvisorParseContext` from the persisted snapshot (or a legacy converter), and call `parse`. It performs no preparation, new consultation, or Bridge transport.

`AnalystOpinionService` loses no responsibility and should gain no parser dependency. It continues independently checking the persisted `operator-advisor-call.v1`, exact v3 draft/review content and hashes, invocation identity, output SHA, and matching input refs before accepting Bridge-authored text (`analyst_opinion_service.py:917-1184`).

## 7. Dependencies and adapters

**In-process dependencies:** the projected prompt data, `build_advisor_dossier`, completeness evaluation, operational-secret guard, and wire syntax parser are local modules. Keep dependencies explicit in the implementation/settings rather than constructing repositories or transports inside it. The module has no database, artifact-store, provider, or network adapter.

**Existing adapters stay in place:** `InvestigationAdvisorTool` adapts tool invocation and persistence; `ProductionArtifactStore` archives prompt and output blobs; the S3 `submit_bridge_consultation_once` function plus its production/scripted `BridgeTurnTransport` adapters own pacing, idempotency, bounded retry, deadlines, and ambiguous outcomes (`bridge_investigator.py:248-495`). `ReparsePersistedAdvisorOutputTool` adapts stored observations/blobs to the same pure parser. The opinion service remains a downstream independent verifier.

The module's normal implementation and deterministic fixtures are enough to exercise the interface; no abstract port for a single interchangeable parser implementation is needed. Production and scripted transports already provide the real two-adapter seam at S3.

## 8. Persistent truth and S3 decision point

There is no new persistent source of truth and no database migration. Continue using the existing content-addressed `advisor-prompts` and `model-outputs` blobs, versioned `operator-advisor-call.v1` observation, persisted Bridge conversation/turn record, and append-only analyst-opinion versions. Raw output SHA and reparse linkage remain unchanged. The new parse-context schema is an additive field inside the existing observation payload; keep legacy `reparse_context` fields during the rollback window so old code can still read newly written observations.

The advisor module emits exact bytes and metadata; it does not decide whether to send. The caller invokes `submit_bridge_consultation_once` exactly once with the prepared prompt. Existing case/sample policy checks remain upstream, `external_llm_allowed=True` is passed only after the current check, and existing transport-side permission/recovery behavior remains downstream. Do not add a second advisor-level send flag, reserve/consume path, retry loop, or “prepared means approved” shortcut. S3 remains the sole submission/idempotency/retry seam; its decision is neither duplicated nor inferred from successful preparation.

## 9. Oracles at the interface

These are design oracles for implementation; **none were run in this design task**.

| Oracle | Required observable result |
|---|---|
| Golden prompt and refs | With a fixed in-memory case/projection fixture, assert exact prompt bytes and SHA, dossier manifest, transmitted refs, alias ledger, offsets, and lengths through the new interface and fake transport. Include raw Base64, large legacy hex/Base64, fragmented values, and contradictory permitted observations; assert evidence bytes survive with their expected identities. Existing leads include `test_600_byte_dns_raw_base64_reaches_fake_transport`, `test_large_legacy_hex_and_base64_measurements_reach_fake_transport`, and `test_contradictory_permitted_dns_evidence_pieces_are_both_transmitted`. |
| Golden parse corpus | Run fixtures for typed v3 success, partial, explicit refusal, invalid/truncated response, v2 JSON, and legacy wire input through `parse`. Assert exact status/schema, warning/ref resolution, no alias fabrication, and stable refusal behavior. Preserve current `review_text` fallback behavior. |
| Policy and secret negatives | Through `InvestigationAdvisorTool.execute`, independently deny `do_not_submit`, `external_llm_allowed=false`, TLP RED, explicit sample restriction, and residual operational secrets; assert zero calls to the fake S3 transport and existing outcome/ref fields. For allowed CTI markers, assert they remain in the captured prompt. |
| Archive/crash edges | Prompt archival failure must still mean no S3 submission. Successful transport followed by raw-output archive uncertainty must retain `needs_review` behavior and must not parse unarchived output. Invalid but successfully returned output remains archived before parse failure. Inject parser failure and assert current adapter error/status mapping, with no raw data in logs. |
| Reparse and replay | Use persisted fixtures for both current and legacy contexts. Verify observation and raw blob SHA/size checks, same Bridge invocation/output SHA/input refs, a new parser-version observation, `reparsed_from_observation_id`, and `bridge_turns_consumed == 0`; assert no transport adapter is reachable from reparse. Existing direct seam lead: `test_reparse_appends_verified_parse_without_a_bridge_transport`; negative lead: `test_reparse_refuses_minio_bytes_that_do_not_match_observation_output_sha`. |
| S3 retry/ambiguity | Existing S3 tests continue to assert deterministic turn identity, bounded retry, and ambiguous result handling. Advisor `parse` must not run for blocked, failed, late, or ambiguous submissions. No test of a fake transport is evidence of provider behavior. |
| Concurrency/idempotency | Concurrent same-key submissions still converge through S3's existing deterministic identity and one durable turn; repeated `prepare` on identical inputs yields the same prompt SHA and ledger. Parser has no shared mutable state. Do not add a second lock or idempotency store in the advisor module. |
| Opinion provenance | Existing opinion-service tests continue to reject stale text/hash, missing real Bridge provenance, mismatched refs, and stale versions; a valid v3 parsed draft remains consumable without changing opinion CAS/version behavior. |

The source audit identified a missing combined policy cross-product oracle and a missing composition-registration oracle for reparse (`M2B_TEST_SURFACE_ADVISOR_AUDIT_PUB.md:23-28, 35-47`). Add those at the caller/composition seams as part of implementation; they are not proof of live transport or MinIO behavior.

## 10. Compatibility, expand/contract migration, and rollback

**Expand 1 — add beside old.** Add the new module and its immutable types while the current tool and reparse paths remain authoritative. Keep the external tool schemas, observation schema `operator-advisor-call.v1`, Bridge keys, and v2/v3 response schemas unchanged. Add interface-level golden prompt, parse, and persisted-context fixtures. No migration or config change.

**Dual path 2 — compare without a second send.** In tests, run old and new preparation/parser implementations over the same deterministic fixtures and historical response corpus; compare exact prompt bytes/hash, reference identities, status, refusal, warning, and normalized output. Keep old behavior authoritative until parity gates pass. Do not issue duplicate Bridge submissions or persist shadow parse output. During a compatibility window, write the new versioned parse-context field additively while retaining old context fields.

**Cutover 3 — normal caller, then reparse.** Switch `InvestigationAdvisorTool.execute` preparation to `prepare` first; S3 submission stays untouched and singular. Once exact prompt/ref parity is established, switch its post-archive parse to `parse`. Then switch `ReparsePersistedAdvisorOutputTool` to the same parser, with a converter for old context shapes and no evidence reload. Keep `AnalystOpinionService` unchanged; keep its real-provenance checks as an acceptance gate for v3 output. If cutover shows any legacy response difference, keep that compatibility branch in `parse` rather than asking each caller to fork behavior.

**Contract 4 — delete old path.** After caller and corpus oracles pass, delete the private preparation/alias aggregation and legacy parse dispatch from `investigation_advisor_tools.py`, plus obsolete duplicate alias/parse helpers in `advisor_contracts.py` / `advisor_wire.py` only when no retained v2/v3 or wire consumer references them. Keep `advisor_dossier`, the secret guard, and `parse_advisor_wire` syntax needed by the central compatibility parser. Move/remove tests that assert private helper internals; retain caller-facing transport, refusal, reparse, and opinion provenance tests.

**Rollback.** Before cutover, revert the new caller wiring; the module can remain unused. After cutover, revert caller wiring to the old implementation while old persisted fields remain present. Do not rewrite or delete immutable prompt/output blobs or versioned observations. Any already-appended parser-version observation is append-only; rollback does not erase it. Reparse remains possible because the additive payload keeps the legacy context shape and the raw output SHA/address. If a new parser version has emitted a bad observation, preserve it and append a corrected version through the existing reparse/versioning path.

**Compatibility risks:** old responses can depend on prompt-era alias conventions; legacy wire warnings/status can differ from v3 normalization; `review_text` intentionally has a distinct fallback; old persisted contexts may omit fields that a new parser expects; additive context can enlarge observation payloads; and downstream opinion provenance requires exact output schema and nested Bridge fields. Parity tests must use archived/synthetic fixtures without transmitting evidence.

## 11. Deletion test

The deletion test is a cutover acceptance gate, not a count-of-lines claim:

1. Temporarily remove the old tool-local prompt/alias/reference preparation and `_parse_advisor_output` / legacy parser-dispatch implementation.
2. Remove the old reparse call to `parse_typed_advisor_response` and any compatibility wrapper that only forwards to the new module.
3. Run the focused interface and adapter oracles in section 9; inspect references to deleted private names and confirm production callers, reparse, and tests cross the new module interface.
4. Confirm `AnalystOpinionService` does not import the new module and still independently validates persisted v3 provenance.

If deleting old code makes the same preparation or parsing decision reappear in either caller, the module is too shallow and the deletion test fails. Keep useful dossier construction and wire syntax modules; delete only truly superseded duplication.

## 12. Caller-by-caller before/after

| Caller | Before (verified) | After (proposed) |
|---|---|---|
| `InvestigationAdvisorTool.execute` — common path | Builds dossier/prompt, alias/reference state and parser context inline; archives prompt; calls S3; archives raw output; passes many correlated values into `_submission_result` / private parser. (`investigation_advisor_tools.py:415-886, 2949-3180`) | One `prepare` call yields exact prompt and one ledger. Archive it, call existing S3 helper once, archive output, then `parse` only for successful outcomes. Existing tool still maps statuses, persists observation, and returns `ToolResult`. |
| `ReparsePersistedAdvisorOutputTool.execute` | Verifies original observation/blob/provenance and directly invokes typed parser; reconstructs legacy context for missing fields. (`investigation_advisor_tools.py:3414-3787`) | Preserve all verification and append behavior; convert persisted current/legacy context to `AdvisorParseContext` and call shared `parse`. No `prepare`, provider, or Bridge dependency. |
| `AnalystOpinionService` | Loads persisted advisor-call observation and independently checks schema, call status, turn ID, hashes, input refs, exact text, block/anchor and review hashes (`analyst_opinion_service.py:917-1184`). | No interface change. It remains an independent trust check and append-only opinion version/CAS owner. |
| Existing tests | Mix pure dossier/parser helper coverage, large evidence/fake-transport tests, direct reparse tests, and opinion provenance tests; M2B notes gaps around whole consultation and composition wiring. | Replace private-helper tests with module-interface tests; retain/extend end-to-end fake-transport, refusal, S3 ambiguity, reparse, and opinion tests. Add composition exposure and cross-product policy gates. |

## 13. Risks, UNKNOWNs, and limits

- **Hardest unresolved risk — historical parse context completeness.** Current reparse accepts selected consultation types and calls only the typed parser, while the normal consultation path contains typed plus legacy branches. Source inspection cannot prove every historical persisted shape has enough aliases, dossier metadata, or prompt-era semantics for a centralized parser to reproduce prior behavior. The old-shape fixture corpus and a fail-closed converter are gating work.
- **UNKNOWN:** exact deployed mix of `advisor-response.v2`/v3, legacy wire, and persisted `reparse_context` versions; size distribution of old contexts; whether all composition roots expose reparse; production artifact adapter behavior. Do not infer these from fake-store tests.
- The current reparse allowlist omits `suricata_draft` although context capture contains it (brief evidence: `investigation_advisor_tools.py:3442-3487, 4188-4225`). Do not expand scope by silently adding the type; decide separately with explicit compatibility tests.
- The current parser/reference helpers answer related but distinct questions. The new ledger must derive aliases from the exact prompt actually sent, and must not widen what evidence was authorized or narrow what permitted CTI evidence remains observable.
- Do not collapse sample policy and case-level disclosure into the advisor module; those inputs and authority differ. Do not treat prepared state as authorization. S3 remains the only submission seam.
- This artifact proposes a code seam and migration only. It does not authorize a provider call, PostgreSQL/MinIO access, schema change, deployment, publication, or external evidence submission.
