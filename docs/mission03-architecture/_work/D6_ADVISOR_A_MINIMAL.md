# D6-ADV-A — Minimal advisor consultation interface

**Design basis:** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`, branch
`mission03/architecture-design`. **Scope:** Mission 03 Phase 1, advisor
consultation preparation and response parsing. This is a design artifact; no
production code or tests were changed or run.

Labels in this artifact mean: **VERIFIED** = visible in the cited source or
test source at the design SHA; **INFERENCE** = proposed architecture or
conclusion from those facts; **UNKNOWN** = not established by source-only
inspection.

## 1. Current and proposed seam

**VERIFIED — current flow.** `InvestigationAdvisorTool.execute` loads and
filters case evidence, applies the case/sample disclosure gates and operational
secret guard, builds the dossier, performs completeness and size preflight,
renders `advisor-wire.v1`, assembles refs/context, archives the prompt, then
calls `submit_bridge_consultation_once` (`investigation_advisor_tools.py:323-778`).
That Bridge seam owns idempotency, pacing, bounded retry and ambiguous
submission outcomes (`bridge_investigator.py:248-495`). Successful raw output
is archived before parsing (`investigation_advisor_tools.py:784-886`). Live
parsing selects typed v3 or legacy v2/wire paths; persisted reparse currently
calls only the typed parser (`investigation_advisor_tools.py:3414-3787,
4745-5065, 5067-5315`).

**VERIFIED — owners to retain.** `advisor_dossier` already owns extraction,
selection, contradiction handling, stable aliases, manifest hashes and
completeness (`advisor_dossier.py:114-301, 304-933, 942-1430`). The secret
guard, `submit_bridge_consultation_once`, model-conversation persistence and
`AnalystOpinionService` have distinct existing responsibilities (M4-A, §1).

**INFERENCE — proposed seam.** Put one pure application module at a candidate
path such as `backend/src/cti_app/application/advisor_consultation.py`. Its
interface has two principal operations:

1. `prepare(authorized_projection) -> PreparationResult`
2. `parse(parse_basis, raw_output_bytes) -> ParseResult`

`prepare` composes the existing dossier, completeness, exact wire rendering,
ref identity and parse-context construction. `parse` is the sole semantic
parser entry for both live responses and stored-output reparse. These are deep
operations: callers receive an exact payload or a typed non-submission result,
and a normalized parse result with provenance, rather than coordinating
several builders and parser branches.

The seam belongs after existing authorization and evidence projection, and
after the generic maximum reservation, but before the caller's S3 dispatch
decision and the existing Bridge transport seam. It does not evaluate or
grant permission, choose whether to dispatch, reserve/consume budget, or
perform transport. The caller remains the application use case, not a thin
adapter for those decisions.

## 2. Public contract and pseudo-code

The names below are proposed types, not verified code. Collections are
immutable snapshots; mappings in a stored record are copied before use.

```python
@dataclass(frozen=True)
class SourceRef:
    kind: str
    id: str
    sha256: str

@dataclass(frozen=True)
class PreparedConsultation:
    prompt_utf8: bytes                 # exact bytes that will be archived/sent
    context_sha256: str                # SHA256(prompt_utf8)
    consultation_type: str
    response_schema: str               # advisor-response.v2 or .v3
    dossier_sha256: str
    dossier_manifest: tuple[ManifestEntry, ...]  # alias, ref, offsets, lengths
    transmitted_refs: tuple[SourceRef, ...]
    alias_catalog: Mapping[str, AliasTarget]    # R/E/M/F/A aliases -> identity
    parse_context: AdvisorParseContext          # captured for live parse/reparse
    preflight: CompletenessResult

    @property
    def prompt_text(self) -> str:
        return self.prompt_utf8.decode("utf-8", errors="strict")

@dataclass(frozen=True)
class PreparationRefusal:
    status: str
    reason: str
    preflight: CompletenessResult | None
    details: Mapping[str, object]
    confirmed_non_execution: bool = True

PreparationResult = PreparedConsultation | PreparationRefusal

@dataclass(frozen=True)
class PreparedParseBasis:
    context: AdvisorParseContext

@dataclass(frozen=True)
class StoredParseBasis:
    # Verified operator-advisor-call.v1 record, including any historical
    # reparse_context; old records may use the existing legacy fallback.
    call_record: Mapping[str, object]

ParseBasis = PreparedParseBasis | StoredParseBasis

@dataclass(frozen=True)
class ParseResult:
    output_sha256: str
    parser_version: str
    response_schema: str
    outcome: str                 # existing success/partial/refused/invalid meaning
    value: Mapping[str, object] | None
    resolved_refs: tuple[SourceRef, ...]
    diagnostics: tuple[ParseDiagnostic, ...]
```

`authorized_projection` means the evidence projection *after* the existing
case-level, sample-level and explicit-reference restrictions have run. The
type is a statement about call order, not an authorization token; a caller
cannot use it to create permission. The module does not accept an
`external_llm_allowed` boolean and cannot submit anything.

```python
def prepare(projection: AuthorizedProjection) -> PreparationResult:
    # No I/O and no policy decision.
    scrubbed = redact_operational_secrets(projection, injected_settings)
    if scrubbed.has_residual_operational_secret:
        return refusal("secret_field_remaining", scrubbed.audit_details)

    dossier = build_advisor_dossier(scrubbed.prompt_data)   # exactly once
    refs, aliases = build_ref_catalog(scrubbed, dossier.manifest)
    prompt_text = render_advisor_prompt(
        scrubbed, dossier.text, dossier.manifest, aliases
    )
    prompt_utf8 = prompt_text.encode("utf-8")
    preflight = evaluate_advisor_evidence_preflight(
        scrubbed, dossier, prompt_text
    )
    if exceeds_configured_transport_limit(prompt_utf8) or not preflight.ready:
        return PreparationRefusal(existing_status_and_reason(preflight), ...)
    return PreparedConsultation(
        prompt_utf8=prompt_utf8,
        context_sha256=sha256(prompt_utf8),
        dossier_manifest=dossier.manifest,
        transmitted_refs=refs,
        alias_catalog=aliases,
        parse_context=make_parse_context(...),
        ...,
    )

def parse(basis: ParseBasis, raw_output_bytes: bytes) -> ParseResult:
    raw_sha = sha256(raw_output_bytes)
    context = resolve_current_or_legacy_context(basis)
    # Parse sanitized text only; the caller has already archived these bytes.
    return semantic_parse_v2_or_v3(context, raw_output_bytes, raw_sha)
```

Preparation errors preserve the existing failure code/status and
`confirmed_non_execution` result; they never silently clip, drop or reformat
evidence to fit. Parsing returns explicit refusal as `outcome="refused"`,
never as a warning-only success. Ambiguous, blocked or failed transport
outcomes do not call `parse`. Parse failure does not remove the archived raw
output.

### Exact reference identity

**INFERENCE — one identity contract.** A source identity is the exact triple
`(kind, id, sha256)`. Within one prepared consultation, `(kind, id)` must map
to one SHA; conflicting digests fail closed rather than overwrite each other.
The manifest retains its selected alias, source identity, offsets, lengths,
provenance and content hashes. Those slice details enrich a reference; they do
not replace its source identity.

Every response alias (`R*`, dossier `E*`, measurement `M*`, network fact `F*`,
assertion `A*`) resolves through the one immutable `alias_catalog` to that
identity or to a typed measurement/assertion entry linked to it. The parser
returns the resolved exact refs; the persistence projection copies those
refs from `PreparedConsultation`, never rebuilding them from source rows.
Normalize article and opinion identifiers once when constructing the catalog
and retain the exact versioned opinion `source_ref` and SHA. Keep legacy alias
spellings as lookup aliases only; do not rewrite persisted source IDs.

**VERIFIED — current need.** `_typed_transmitted_refs`,
`_transmitted_refs_for_dossier`, `advisor_wire_aliases`, and parser alias
resolution answer related but different identity questions (M4-A, duplication
table; current source at `investigation_advisor_tools.py:4453-4505,4630-4648`
and `advisor_wire.py:90-264`). This is the specific cluster the shared
catalog replaces.

## 3. Invariants, timing, versions, errors and confidentiality

- **VERIFIED timing:** reservation uses registered maxima before the adapter;
  exact prompt, refs and hash are known later, after reservation and before
  transport (M4-A, §“Advisor preparation timing and reparse”). `prepare` sits
  at that existing point. It cannot change the reservation or decide to
  dispatch.
- **INFERENCE:** `context_sha256` is exactly SHA-256 over `prompt_utf8`.
  Archive and Bridge transport consume that same value; no caller re-renders,
  normalizes line endings or recomputes refs after preparation. The S3 seam
  derives its existing idempotency key from the same hash and turn identity.
- `advisor-wire.v1` and `advisor-consult.v1` remain stable. Typed consultation
  profiles retain `advisor-response.v3`; compatible legacy profiles retain
  `advisor-response.v2`. Parser implementation version is recorded separately
  (a changed implementation should receive a new parser version).
- Permitted CTI evidence stays permissive: raw/hex/Base64/bytes, network data,
  IOC values, long excerpts and contradictory pieces remain available to
  selection and manifest logic. No new 96 KiB, 512-byte or 16 KiB ceiling,
  field-name ban, or silent truncation is introduced. The configured prompt
  limit remains a transport/preflight constraint; editorial batches may reduce
  a coherent unit, not erase part of its evidence.
- G2/G3, TLP RED, `external_llm_allowed=false`, `do_not_submit`, sample/source
  restrictions and revocation remain enforced by their current owners before
  this seam. The `publication_secret_guard` still redacts AutoWork operational
  credentials and refuses residuals. Its settings are injected into the
  implementation; the codec does not invent a second secret policy.
- The raw model output remains an archived consultation artifact,
  `primary_evidence=false`; parser output is untrusted and is not canonical
  CTI evidence. `AnalystOpinionService` continues validating exact Bridge
  provenance, citations, expected opinion version and content SHA before an
  opinion version is appended (`docs/investigation_contracts.md:37-47`).
- A persisted reparse verifies the stored output hash and context before
  semantic parsing, does not reload canonical evidence and consumes zero new
  Bridge turns. A new parser version produces a separately versioned parse
  observation; it never mutates the old raw output or old observation.
- Preserve current result distinctions: explicit refusal, partial response,
  invalid/unintelligible output, preflight refusal, blocked/failed transport,
  and ambiguous external state. Unknown/ambiguous aliases cannot become valid
  refs. Sanitization for Bridge/UI marker lines affects parser input only.
- Prepared bytes and CTI evidence are confidential. Do not log or trace prompt
  bytes, raw output, operational credentials or unnecessary evidence.

**UNKNOWN:** no single documented version registry currently ties together
`advisor-consult.v1`, `advisor-wire.v1`, v2/v3 response schemas and
`advisor-source-parser.v3` (M4-A, §UNKNOWNs). The exact proposed parser-version
label and stored parse-context schema need a compatibility decision before
implementation.

## 4. Example from the real consumer

**INFERENCE — application use case pseudocode.** Repository reads, explicit
policy gates, source filtering, operation reservation and invocation
bookkeeping stay where they are. The consumer supplies the resulting
projection, then owns the dispatch decision and persistence effects:

```python
projection = await load_and_filter_advisor_projection(...)  # current gates
prepared = advisor_consultation.prepare(projection)
if isinstance(prepared, PreparationRefusal):
    return await persist_existing_preflight_result(prepared)

prompt_blob = await artifact_store.put_bytes(
    prepared.prompt_utf8, bucket="advisor-prompts"
)
submission = await submit_bridge_consultation_once(
    transport,
    investigation_id=context.investigation_id,
    subject_id=investigation.subject_id,
    turn_index=turn_index,
    context_sha256=prepared.context_sha256,
    prompt=prepared.prompt_text,
    external_llm_allowed=True,  # only after the existing policy gate
    ...,
)
if submission.status is not SUCCEEDED:
    return existing_submission_outcome(submission)  # ambiguity is not parsed

raw = submission.output_text.encode("utf-8")
output_blob, output_sha = await archive_raw_output_before_parsing(raw)
parsed = advisor_consultation.parse(PreparedParseBasis(prepared.parse_context), raw)
return await persist_existing_call_observation(
    prepared=prepared, parsed=parsed, output_blob=output_blob, output_sha=output_sha
)
```

The real consumer passes the same exact prompt/hash to the existing S3 seam;
only the seam's existing transport adapter submits it. For reparse, the tool
reads and verifies the stored blob/observation as it does today, then passes
the stored `operator-advisor-call.v1` record and those exact bytes to
`parse(StoredParseBasis(record), raw_bytes)`. The codec does not call Bridge.

## 5. Responsibilities hidden in the implementation

The implementation composes, behind the two-operation interface:

- one dossier build, manifest, selected text, byte count and completeness
  result; it passes that same manifest/text into alias rendering instead of
  the currently redundant second `build_advisor_dossier` call
  (`advisor_wire.py:203-228`);
- operational-secret projection and residual refusal using the existing
  guard, not generic CTI-key redaction;
- exact UTF-8 prompt construction and SHA calculation;
- one canonical source-ref catalog and response alias index, including
  collision detection, stable order, article/opinion normalization, and
  manifest offsets/lengths/provenance;
- semantic dispatch between current typed v3 and legacy v2/wire forms,
  shared wire lexing, safe text rules, refusal preservation, warning and
  normalization collection, partial review handling, and parse diagnostics;
- construction/validation of the versioned parse context needed to replay the
  exact aliases and per-unit targets without current source reads. Old stored
  call records retain their legacy context reconstruction inside this
  implementation.

The module does not own canonical repository/blob reads, the external
permission decision, sample restriction policy, invocation/budget state,
prompt/raw-output persistence, transport, or opinion writes. `advisor_dossier`
and its pure functions remain internal collaborators, not a replacement
candidate.

## 6. Caller responsibilities removed and retained

**Removed from live and reparse callers:** separately assembling transmitted
refs, recomputing aliases, translating aliases to source IDs, choosing typed
versus legacy semantic parser paths, implementing different parse/refusal
semantics for live parse and reparse, and rebuilding old reparse context from
response units. Both paths use one ref catalog and parser contract.

**Retained at the application use case:** request validation; canonical
repository reads and evidence projection; policy/revocation/sample gates;
the generic reservation and dispatch choice; editorial batching and turn
accounting; archive writes; Bridge outcome handling; observation persistence;
and calling `AnalystOpinionService` only through its existing provenance and
version checks.

For an editorial multi-unit request, each actual Bridge dispatch gets one
`PreparedConsultation` and its own prompt hash/ref set. Existing orchestration
chooses/schedules units and batches because that changes dispatch count,
budget use and ambiguity handling. It calls `prepare` per selected batch and
retains each unit ID in that batch's parse context; the existing result
combiner remains. This deliberately keeps the dispatch decision out of the
codec while preventing callers from rebuilding each batch's bytes or refs.

## 7. Dependencies and adapters

**VERIFIED dependency categories (DEEPENING):** dossier selection, wire
serialization, alias resolution and semantic parsing are in-process pure
work. The codec can be tested directly without a new port. Secret settings
are injected into its implementation. Canonical blobs/repositories are
owned by the caller and existing adapters.

The transport is a separate seam with existing production and scripted/fake
adapters: `ModelConversationTurnTransport` and test transports satisfy the
Bridge transport role (M4-A, §1). Keep `submit_bridge_consultation_once` as
the owner of pacing, idempotency, bounded retry and ambiguous outcomes. It
accepts the existing explicit policy result; it does not calculate policy.
Do not insert a new transport adapter into the preparation/parser module.

## 8. Persistent source of truth and the S3 decision

**VERIFIED:** canonical investigation observations and versioned artifacts
remain authoritative; prompt/output blobs have SHA identities. A successful
call observation records input/output SHAs, refs, reparse context and Bridge
provenance. Model-conversation output is immutable but not primary evidence
(M1-B §call graph; `docs/model_conversations.md:1-21,63-99`). Opinion content
is append-only/versioned with `content_sha256`
(`docs/investigation_contracts.md:37-47,79-129`).

**INFERENCE — no second source of truth.** `PreparedConsultation` is a
short-lived immutable value, not a persisted canonical record. The caller
archives its exact prompt bytes in the existing `advisor-prompts` store and
persists refs/context into the existing `operator-advisor-call.v1` record.
The output blob remains addressed by its SHA in `model-outputs`. There is no
new table or independent cache. The stored prompt bytes, their SHA, Bridge
`context_sha256`, prepared refs, and reparse context all derive from the same
prepared value.

The S3 decision stays between preparation and
`submit_bridge_consultation_once`: the caller accepts a ready preflight or
persists the current non-execution outcome. Only after it chooses dispatch
does it archive and submit. S3 continues deriving its deterministic turn key
from investigation, turn index and the prepared context hash. It owns the
transport effects, not the prepared ref set or authorization policy.

## 9. Positive, negative, crash, replay and concurrency oracles

These are proposed acceptance oracles; none was run for this design.

- **Golden preparation:** for frozen authorized inputs, compare exact UTF-8
  prompt bytes, prompt SHA, dossier SHA, ordered manifest (including offsets
  and lengths), transmitted `(kind,id,sha256)` refs and alias catalog. Include
  long raw/fragmented Base64, hex, network facts, both sides of a contradiction
  and generic CTI `api_key`. Existing source tests include
  `test_600_byte_dns_raw_base64_reaches_fake_transport`,
  `test_spaced_fragmented_600_byte_base64_reaches_fake_transport`,
  `test_contradictory_permitted_dns_evidence_pieces_are_both_transmitted`,
  and `test_generic_api_key_in_cti_evidence_is_not_an_autowork_secret`
  (M2-B, advisor rows; M4-A §tests).
- **Negative policy matrix:** for the same evidence fixture, independently
  apply external=false, `do_not_submit`, TLP RED and a restricted explicit
  sample ref. Assert the existing policy status/refusal survives and there is
  zero call to the fake transport. These gates must run before preparation
  can create a dispatchable value. Preserve the separate secret-redaction
  positive case and prove adjacent permitted CTI text remains.
- **Parser corpus:** freeze v2 JSON, v2 wire, typed v3 JSON/wire, typed refusal,
  partial review, malformed output, repeated/ambiguous aliases, unknown refs,
  and each consultation type. Assert exact outcome/status, parser/schema
  version, diagnostics and resolved source triples. An explicit refusal stays
  refused; no alias can point to an untransmitted ref.
- **Reparse:** feed verified stored output bytes and old/new call contexts to
  the same `parse` operation. Assert output SHA and Bridge identity are
  unchanged, no repository evidence read and no transport call occurs,
  `bridge_turns_consumed == 0`, and a new parser version appends a distinct
  parse observation linked by `reparsed_from_observation_id`. Same-version
  replay is idempotent. Existing source tests include
  `test_reparse_appends_verified_parse_without_a_bridge_transport`,
  `test_reparse_refuses_minio_bytes_that_do_not_match_observation_output_sha`
  and `test_reparse_salvages_complete_review_units_from_partial_wire_without_bridge`
  (M2-B §reparse row; M4-A §tests).
- **Crash ordering:** prompt archive failure yields confirmed non-execution
  and zero transport calls; blocked/ambiguous transport never parses; a large
  invalid raw output remains archived before parse rejection; hash/context
  mismatch in reparse fails closed without an observation append.
- **Replay and concurrency:** two equivalent preparations produce byte-identical
  prompts/ref order/hash. Replaying the same S3 turn uses the existing stable
  idempotency identity. Concurrent same-version reparses append at most one
  identical observation; a new parser version remains a separate append.
- **Opinion gate:** pass a parsed draft with valid and forged/stale Bridge
  refs to the existing opinion service test surface; only persisted, exact
  call/turn/output/text refs with expected opinion version/content SHA may be
  appended. Do not treat parser success as an opinion write.

## 10. Compatibility, migration and rollback

No database migration is proposed. Preserve tool/wire versions and output
record shape. Keep parsing old `advisor-response.v2`/wire calls and typed
`advisor-response.v3`; continue shared legacy wire tokenization. The stored
parse context is versioned for newly prepared calls, while old
`operator-advisor-call.v1` records without it continue through the existing
legacy reconstruction, moved inside the codec implementation.

Before cutover, require the frozen v2/v3 corpus to pass through both the
current and proposed interface, including refusal and alias-ambiguity cases.
Use exact prompt golden comparisons for unchanged `advisor-wire.v1` fixtures.
Retain the old parser dispatcher as a rollback implementation until replay
coverage includes historical consultation types and persisted outputs. A
rollback changes module wiring only; it does not rewrite stored prompt/output
blobs or observations. A new parser version adds a reparse observation and
does not replace an earlier interpretation.

**UNKNOWN:** current historical fixture coverage is not established as a
complete v2/v3 corpus. M4-A explicitly identifies this missing oracle and
high migration risk. Do not remove the old parser branches until that corpus
and acceptance policy exist.

## 11. Deletion test

Delete the proposed codec and leave its callers otherwise unchanged. The
following complexity must reappear in both live consultation and reparse:

1. prompt/dossier rendering, byte/hash calculation and completeness coupling;
2. manifest-to-transmitted-ref filtering and article/opinion identity handling;
3. `R/E/M/F/A` alias construction and response alias resolution;
4. typed-v3 versus legacy-v2 semantic dispatch, refusal/warning consistency,
   and old parse-context reconstruction;
5. synchronization between the live parse context, persisted `input_refs`,
   raw output SHA and reparse result.

That work currently spans `investigation_advisor_tools.py`,
`advisor_wire.py` and `advisor_contracts.py` (M4-A, duplication table). If
deleting the codec merely removes a forwarding wrapper and none of these
obligations return across the two callers, the module has no depth and should
not be introduced. Do not use deletion of `advisor_dossier`, S3, or opinion
versioning as the test: those modules already own separate useful behavior.

## 12. Before/after comparison with verifiable evidence

| Concern | Before — verified at design SHA | After — proposed inference |
|---|---|---|
| Prompt preparation | `execute` assembles dossier, manifest, refs, prompt, size and hash across `:436-778`; actual archive follows at `:741-763`. | One `prepare` returns immutable bytes/hash/manifest/refs/context, or an explicit non-submission result. |
| Ref identity | Separate helpers answer typed, dossier-visible and alias-visible ref questions (`:4453-4505,4630-4648`; `advisor_wire.py:90-264`). | One catalog maps aliases to exact source triples and the same triples feed persistence and parsing. |
| Dossier alias input | `advisor_wire_aliases` rebuilds a dossier even when a manifest was supplied (`advisor_wire.py:203-228`; M4-A). | The one prepared manifest/text is reused; no repeated dossier build. |
| Parse behavior | Live dispatcher reaches typed and legacy paths; reparse uses typed parser only (`:3629-3637,4745-5315`). | The same `parse` contract selects the stored/current schema and preserves refusal, partial and invalid outcomes. |
| Reparse obligations | Tool validates bytes and rebuilds/falls back to context before calling parser (`:3414-3787`). | Tool still loads and verifies storage, then supplies the stored record and bytes; codec owns semantic context/alias compatibility and never consults Bridge. |
| Trust and persistence | Opinion service revalidates persisted provenance; output and opinions are versioned separately. | Unchanged. The new seam cannot promote output or author an opinion. |

## 13. Risks, unknowns and limits

- **Hardest unresolved risk — UNKNOWN:** no frozen historical v2/v3 response
  corpus proves that unifying live parse and reparse preserves every persisted
  interpretation. A parser consolidation could alter status, aliases, safe
  text, partial review salvage or refusal semantics. M4-A identifies this as a
  high migration risk; keep the old path until the corpus gate is met.
- **UNKNOWN:** source-only inspection does not prove every article/opinion
  manifest entry has the same exact identity as its transmitted ref. Golden
  preparation must include those identities and resolve all current alias
  forms before implementation.
- **UNKNOWN:** current optional sample-list adapter behavior has a possible
  fail-open edge noted in M4-A. This proposal does not replace or certify that
  policy gate; resolve it in its owner before relying on a prepared projection.
- **INFERENCE / limit:** editorial batching remains in the use case because
  it determines dispatch count and ambiguity recovery. The codec guarantees
  one exact prepared context per dispatch, not a cross-batch transaction.
- **INFERENCE / limit:** reservation remains based on configured maxima before
  exact prompt size is known. This design does not optimize reservation timing.
- The codec interface is not an authorization mechanism. Only the existing
  governed call path may provide its post-filter projection and decide whether
  dispatch is allowed.
- The sibling P10 proposal parser/refusal behavior is a different seam and is
  outside this proposal; do not silently route P10 through this codec.
- No tests, providers, PostgreSQL, MinIO, Docker or network CTI were run, as
  required by the design brief.
