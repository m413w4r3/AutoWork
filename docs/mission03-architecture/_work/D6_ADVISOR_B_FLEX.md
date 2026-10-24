# D6-ADV-B — Advisor interface design: flexibility and extension

**Design point:** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`, branch
`mission03/architecture-design`. Design only; no tests or runtime services were
run.

**Evidence labels:** **VERIFIED** means visible in current source at the stated
SHA; **INFERENCE** means a design conclusion from those facts; **UNKNOWN** means
the current source or retained artifacts do not establish the answer.

## 1. Current seam and proposed seam

**VERIFIED.** `InvestigationAdvisorTool.execute` currently validates all seven
consultation type strings, loads case evidence, filters restricted inputs,
redacts operational secrets, builds the dossier, creates aliases and prompt,
archives prompt and output, dispatches parsing, and persists the call record
(`backend/src/cti_app/application/investigation_advisor_tools.py:164-778`).
The `advisor_dossier` functions already own extraction, selection, manifest,
and completeness (`advisor_dossier.py:304-939,942-1687`) and are pure. The S3
transport seam is `submit_bridge_consultation_once`;
`bridge_investigator.py:248-495` owns idempotency, permission recheck, pacing,
deadline handling, and bounded retry. The opinion service separately verifies
Bridge draft/review provenance before appending a version
(`analyst_opinion_service.py:917-1028`).

**INFERENCE — proposed seam.** Introduce one in-process
`AdvisorConsultationModule` at the point between the tool's already-authorized,
filtered case projection and S3. Its small public interface is `prepare`,
`parse`, and `reparse`. `prepare` owns deterministic source identity, dossier
assembly orchestration, exact wire bytes, and parse context. `parse` owns
version dispatch and normalization. `reparse` owns validation of the stored
context/output pair and calls the same parser implementation. The module does
not own case permission, budget reservation/consumption, revocation, transport,
canonical evidence, or opinion versioning.

Keep `advisor_dossier`, `publication_secret_guard`,
`submit_bridge_consultation_once`, and `AnalystOpinionService` as separate deep
modules. Do not turn consultation variants into dynamically registered
plugins: the extension seam is a closed, explicit tagged union whose variants
are added with a real caller and a versioned response contract.

## 2. Public contract and variant/consumer matrix

**INFERENCE — proposed types.** The names below are illustrative Python
pseudocode; the contract includes ordering and failure behavior, not just
annotations.

```python
ConsultationType = Literal[
    "plan_investigation", "pivot_strategy", "draft_text", "review_text",
    "yara_draft", "suricata_draft", "figure_plan",
]

@dataclass(frozen=True)
class SourceRef:
    kind: str                 # observation, finding, article, measurement, ...
    id: str                   # canonical source identity
    sha256: str               # bytes/content identity for this source version
    aliases: tuple[str, ...]  # current E#/A# and retained legacy aliases
    byte_offset: int | None = None
    byte_length: int | None = None

@dataclass(frozen=True)
class PreparedConsultation:
    consultation_type: ConsultationType
    prompt_utf8: bytes        # the exact bytes passed to S3
    prompt_sha256: str
    wire_version: str        # advisor-wire.v1
    response_schema: str     # advisor-response.v2 or advisor-response.v3
    parser_version: str
    source_refs: tuple[SourceRef, ...]
    dossier_manifest: tuple[Mapping[str, object], ...]
    dossier_sha256: str
    reparse_context: Mapping[str, object]
    unit_plan: tuple[EditorialUnit, ...] = ()

@dataclass(frozen=True)
class EditorialUnit:
    unit_id: str
    form: str
    block_type: str
    anchor: str | None
    assertion_ids: tuple[str, ...]
    reviewed_text_sha256: str | None = None

@dataclass(frozen=True)
class UnitOutcome:
    unit_id: str
    status: Literal["succeeded", "partial", "refused", "invalid", "unprocessed"]
    text_sha256: str | None
    output_sha256: str | None
    bridge_invocation_id: str | None

NormalizedResponse = (
    PlanResponseV2 | PivotResponseV2 | DraftResponseV3 | ReviewResponseV3 |
    YaraResponseV3 | SuricataResponseV3 | FigurePlanResponseV3
)

@dataclass(frozen=True)
class ParseOutcome:
    status: Literal["succeeded", "partial", "refused", "invalid"]
    response_schema: str
    parser_version: str
    normalized: NormalizedResponse | None
    raw_output_sha256: str
    warnings: tuple[str, ...]
    limitations: tuple[str, ...]
    provenance_eligible: bool
    unit_outcomes: tuple[UnitOutcome, ...] = ()

class AdvisorConsultationModule:
    def prepare(self, request: ConsultationRequest,
                projection: AdvisorProjection) -> PreparedConsultation: ...
    def parse(self, prepared: PreparedConsultation,
              raw_output: bytes) -> ParseOutcome: ...
    def reparse(self, archived: ArchivedConsultation,
                raw_output: bytes) -> ParseOutcome: ...
```

`ConsultationRequest` is a discriminated union over the seven listed values;
`AdvisorProjection` is the case-specific material after existing permission,
sample restrictions, source verification, and secret checks. `prepare` may
return a typed local refusal (policy, completeness, invalid projection, or
configured transport-size admission) instead of a prepared value. It may not
clip evidence. Any field omitted by projection or dossier selection is
represented in the omission/completeness manifest with a reason. The existing
dossier builder remains the implementation for selection and completeness;
the new module should make its decisions and source identities available
through one interface.

| Variant / exact type | Real consumer and input | Response contract | Output and provenance use |
|---|---|---|---|
| Initial plan — `plan_investigation` | Operator/agent asks for a first investigation plan from case snapshot, article passages, evidence, samples, gaps and available tools (`investigation_advisor_tools.py:1605-1648,2113-2125`). | Legacy `advisor-response.v2` JSON or `advisor-wire.v1`; v2 wire contract covers summary, alternatives, experiment, leads, recommendations, risks/gaps (`:4538-4605,4689-4696,4745-4808`). | Untrusted planning output; never a finding or permission. |
| Plan update — `pivot_strategy` | Operator/agent requests a pivot using investigation state and selected verified measurements (`:2216-2228`). | Legacy v2 JSON/wire. | Untrusted suggestions; evidence references retain their source SHA. |
| Editorial draft — `draft_text` | Operator prepares one or more selected opinion blocks/assertions (`:1816-1850` and `:200-206`). | Typed `advisor-response.v3`, with wire v1 support; blocks contain sentences, assertion refs, block/form/anchor metadata (`advisor_contracts.py:52-103,692-859`). | Only exact v3 draft text with a valid call can provide `BridgeDraftReferenceV1(invocation_id, text_sha256)` to the opinion service. |
| Editorial review — `review_text` | Operator requests review of a specific opinion version and body or anchored analyst note (`:1665-1815`). Multiple units are accepted. | Typed v3 primary; legacy v2 compatibility path remains in `_parse_advisor_output` for review (`:4745-4775`). | Reviews retain `unit_id`, reviewed text SHA, remarks and source refs. Only the v3 review path can satisfy current opinion provenance checks. |
| Rule draft — `yara_draft` | Operator selects verified measurements; rules and motif provenance are advisory (`:2126-2159`). | Typed v3 JSON or wire; measurement IDs and SHA overrides bind motifs to evidence (`advisor_contracts.py:78-84,820-960`). | Untrusted rule text with measurement IDs/SHA; it does not register or execute a rule. |
| Network rule draft — `suricata_draft` | Operator asks for a rule from observed network/protocol facts (`:2160-2186`). | Typed v3 JSON or wire (`advisor_contracts.py:85-86,860-960`). | Untrusted rule and fact references; no packet meaning or execution is inferred. Reparse eligibility is currently missing (see §13). |
| Figure plan — `figure_plan` | Operator selects findings and asks for a structured diagram plan (`:2187-2215`). | Typed v3 JSON or wire; figure schema is separately domain-typed (`advisor_contracts.py:87-88`; `domain/advisor_figure_plan.py`). | Untrusted node/edge plan with selected finding and observation provenance; no renderer code or canonical figure mutation. |

The current typed set is exactly `draft_text`, `review_text`, `yara_draft`,
`suricata_draft`, and `figure_plan`; current response schema selection uses v3
for those and v2 otherwise (`investigation_advisor_tools.py:78-83,4689-4696`).
The matrix calls `plan_investigation` the initial plan and keeps
`pivot_strategy` as a distinct plan update because both are accepted request
types and have separate consumers.

## 3. Invariants, timing, versions, errors, and confidentiality

**VERIFIED.** The exact rendered prompt is UTF-8 encoded and hashed before S3;
the prompt is archived before submission. A successful provider output is
archived before parsing. Ambiguous, blocked, and unsuccessful S3 outcomes do
not reach normal parsing (`investigation_advisor_tools.py:436-778,784-886,
2949-3168`; `bridge_investigator.py:248-495`).

**INFERENCE — contract invariants.**

- `prompt_sha256 == sha256(prompt_utf8)`; S3 receives the same bytes decoded as
  UTF-8, and the stored input hash is that exact prompt identity.
- Every transmitted alias resolves to one canonical `(kind, id, sha256)`;
  IDs, SHA-256, byte offsets/lengths when present, aliases, dossier manifest,
  batch/unit IDs, and Bridge provenance survive normalization and persistence.
- Raw output remains a consultation artifact (`operator-advisor-call.v1` and
  `model-outputs`), marked untrusted; it is never promoted to canonical CTI
  evidence or a detection candidate by this module.
- Authorized CTI content remains permissive: raw bytes, hex, Base64, network
  records, attacker tokens, long excerpts and contradictions are not rejected
  merely for shape or size. No fixed evidence ceiling or silent truncation is
  added. Existing configured prompt admission and Bridge file transport remain
  transport concerns. AutoWork credentials, signed URLs, `do_not_submit`,
  external-disclosure denial, TLP RED, G2/G3 and sample restrictions still
  refuse or filter before S3.
- `refused` remains a first-class outcome, including its reason. It is never
  reduced to a warning or to partial success. `partial` carries usable parts
  and warnings; `invalid` carries a safe rejection code while the archived raw
  bytes and SHA remain available. Parser exceptions become invalid/partial
  according to the existing caller contract, not success.
- Parse only after S3 reports `SUCCEEDED` and the raw output archive and hash
  verify. A timeout with external state unknown is `needs_review`, not a parse
  input and not permission to send again.
- Keep `advisor-consult.v1`, `advisor-wire.v1`, outer call schema
  `operator-advisor-call.v1`, response schemas v2/v3, parser version, and
  opinion schema/version identities distinct. Never rewrite an old response
  as v3 just because the current parser is v3.

The parsing outcome distinguishes `response_schema` from `parser_version`:
the former records the wire/JSON contract in the raw response; the latter
records which local implementation normalized it. That distinction permits a
new parser to read old v2 output without claiming the provider emitted v3.

## 4. Example from a real consumer

**INFERENCE — call sequence for `draft_text`.** The operator-facing tool keeps
request validation and authorization. It builds a filtered projection, then
uses the proposed module for preparation and parsing. S3 remains the only
transport path. A separate, human-mediated opinion command can consume only
the persisted v3 provenance that `AnalystOpinionService` already validates.

```python
projection = await collect_and_filter_case_inputs(request, context)
prepared = advisor.prepare(request, projection)
archive_prompt(prepared.prompt_utf8, prepared.prompt_sha256)

submission = await submit_bridge_consultation_once(
    transport,
    investigation_id=context.investigation_id,
    subject_id=context.subject_id,
    turn_index=context.turn_index,
    context_sha256=prepared.prompt_sha256,
    prompt=prepared.prompt_utf8.decode("utf-8"),
    external_llm_allowed=projection.external_llm_allowed,
)
if submission.status is not TurnSubmissionStatus.SUCCEEDED:
    return preserve_transport_outcome(submission)  # no parse for ambiguous/blocked

raw = await archive_and_verify_raw_output(submission)
outcome = advisor.parse(prepared, raw)
call = persist_operator_advisor_call_v1(prepared, submission, outcome)
# An operator may later select an exact block and cite call ID + text SHA.
```

`AnalystOpinionService` requires `BridgeDraftReferenceV1` to match the draft
text SHA and the saved call's v3 output; review provenance also binds the call's
output SHA and the reviewed text SHA (`domain/analyst_opinion.py:197-215`;
`analyst_opinion_service.py:917-1028,1029-1127`). The opinion service remains
the only module that composes, revises, validates citations, and appends an
`AnalystOpinionV1` version.

## 5. Responsibilities hidden in the implementation

The proposed module centralizes the variant-to-contract dispatch, exact
UTF-8 prompt/hash calculation, current and legacy alias resolution, response
schema dispatch, safe normalization, partial/refusal/invalid classification,
and construction of reparse context. It calls the existing dossier and wire
rendering modules rather than duplicating their rules. It also binds a
multi-unit editorial result to each unit's original identity and reviewed or
drafted text SHA.

The application adapter continues to load canonical evidence, verify blob
descriptors, enforce source restrictions and disclosure, pass the effective
permission to S3, archive blobs, and record an `EvidenceObservation`. This
keeps DB/blob I/O out of the parser and prevents a pure preparation seam from
becoming a new authorization path.

For editorial batching, `unit_plan` preserves the caller's `unit_id`, form,
block type, anchor, assertion IDs, and (for review) exact reviewed-text SHA.
Size admission may partition that plan into deterministic child preparations;
each child records `batch_index`, `batch_count`, child idempotency key, and its
unit IDs. `unit_outcomes` joins each result back to its unit and child Bridge
invocation. An output cursor identifies `next_batch_index` and
`unprocessed_unit_ids`. This represents the current split-and-combine behavior
(`investigation_advisor_tools.py:435-478,1203-1356`) without flattening distinct
editorial targets into one untraceable string.

## 6. Caller responsibilities removed

The consultation tool and reparse tool no longer each maintain their own
response-schema switches, alias lookup rules, result warning classification,
typed fallback behavior, source-ref serialization, and parser-version
selection. The caller supplies a consultation type, validated arguments, and
the already-filtered projection; it consumes `PreparedConsultation` and
`ParseOutcome`. The S3 caller no longer needs a separate reconstruction of
which references the prompt actually transmitted.

Policy admission, exact budget accounting, deterministic transport identity,
raw artifact storage, and opinion citations do not disappear from their
current owners.

## 7. Dependencies and adapters

| Dependency | Category | Proposed use |
|---|---|---|
| Dossier selection, wire rendering, v2/v3 parsers | In-process | Existing implementations behind `AdvisorConsultationModule`; direct pure tests use the same interface. |
| Investigation/evidence/opinion repositories and blob reads | Local-substitutable | Remain in the application adapter. Production repositories/blob store and existing in-memory fakes are already distinct implementations for tests. |
| S3 `submit_bridge_consultation_once` + `BridgeTurnTransport` | Remote but owned | Existing seam stays; production `ModelConversationTurnTransport` and scripted test transports remain adapters. The advisor module is not a transport adapter. |
| `AnalystOpinionService` / PostgreSQL UOW | Local-substitutable / canonical persistence | Existing service and adapters stay; the module supplies validated, versioned provenance only. |

There is no new external port or plugin registry. One pure module is sufficient
because its inputs and outputs are local values; existing I/O seams already
have production and test adapters.

## 8. Persistent source of truth and S3 decision point

PostgreSQL observations/opinion versions and their referenced MinIO blobs
remain canonical. The prompt archive is immutable consultation input; the raw
model output is a consultation artifact; the parser output is an untrusted
projection. `operator-advisor-call.v1` retains the S3 invocation ID, input and
output hashes, actual source refs, parser metadata, dossier/completeness data,
and reparse context (`investigation_advisor_tools.py:901-1071`). It does not
turn model text into canonical facts.

Preparation finishes before S3, so the exact prompt, refs, aliases, manifest,
and prompt hash exist before the decision to submit. The current tool still
owns admission and `submit_bridge_consultation_once` is the final S3 point:
it rechecks `external_llm_allowed`, computes stable idempotency, paces the
request, bounds retry/deadline, and returns ambiguous state without parsing
(`bridge_investigator.py:248-495`). The new module neither consumes budget nor
changes permission/revocation semantics. The transport adapter persists/reuses
the durable turn; raw output is archived before parse.

## 9. Parse, reparse, failure, and concurrency oracles

These are **proposed oracles only**; no tests were run. M2-B reports existing
fake-transport parsing, refusal, large-output archive, and direct reparse
coverage, while calling for a combined permissive-evidence/explicit-denial
matrix and a production-adapter reparse check
(`M2B_TEST_SURFACE_ADVISOR_AUDIT_PUB.md:19-47`).

1. **Variant goldens:** for every matrix row, prepare the real request shape
   and assert exact prompt bytes/SHA, response schema, parser version, source
   IDs/SHA, aliases, offsets/lengths, and dossier manifest. Golden files cover
   current JSON and `advisor-wire.v1` output shapes.
2. **Positive permissive evidence:** pass authorized raw/hex/Base64/network
   evidence and contradictory excerpts through preparation; assert bytes or
   exact text and source SHA/length survive. Separately assert that configured
   transport size admission is observable and never silently drops facts.
3. **Negative policy and refusal:** vary `external_llm_allowed=false`,
   `do_not_submit`, TLP RED and sample restriction independently; assert zero
   S3 calls. Parse explicit `STATUS: refused` and typed v3 refusal with extra
   candidate-like content; assert outcome remains refused, unusable, and
   cannot produce opinion provenance. Malformed schema, unknown type, bad
   hash, unresolved alias, wrong consultation type, missing required field,
   and truncated v3 output are invalid/partial as specified, never success.
4. **Partial and batching:** force a multi-unit editorial prompt over the
   configured transport threshold. Assert deterministic child keys, stable
   `unit_id` and text SHA mapping, batch index/count/cursor, exact source refs,
   and per-unit outcomes. When a child refuses or fails, prior consumed turns
   stay charged; refused units remain refused, and later units are explicitly
   unprocessed. No aggregate success may hide a child refusal.
5. **Crash/replay at S3:** interrupt after prompt archive, during an ambiguous
   turn, after durable raw-output archive, and before call-record append.
   Assert same durable turn/idempotency identity is recovered, ambiguous output
   is never parsed, successful output is not submitted twice, raw SHA is
   unchanged, and the final call observation has the matching input/output
   hashes.
6. **Reparse:** for stored v2 and v3 records, assert blob descriptor SHA/size
   and output SHA are verified, reparse uses the stored context and alias map,
   the raw output and original observation remain immutable, the appended
   observation points to `reparsed_from_observation_id`, and Bridge turns
   consumed are zero. Add `suricata_draft` only when its stored context has the
   exact network facts and refs. If legacy context cannot be verified, return
   a reasoned non-usable outcome without loading fresh evidence or calling S3.
7. **Opinion provenance:** for v3 draft/review, pass exact call ID, output SHA,
   text SHA, unit ID, form, block type and anchor through
   `AnalystOpinionService`; assert a matching persisted call is accepted and
   mismatched call/output/text or a v2 result is rejected. Assert opinion
   revision remains append-only and content SHA/version checks are unchanged.
8. **Concurrent replay/reparse:** run two submissions with the same operation
   identity and two reparses with the same original observation + parser
   version. Assert S3 idempotency prevents a second external turn and the
   existing `advisor-reparse:<observation>:<parser-version>` idempotency key
   yields one equivalent appended observation, not conflicting lineage.
   Verify mixed batches do not duplicate completed child keys while resuming
   from the persisted cursor.

## 10. Compatibility, rollout, and rollback

No database migration is proposed. Keep the current `advisor-consult.v1`,
`advisor-wire.v1`, `operator-advisor-call.v1`, and `analyst-opinion.v1` outer
contracts. Add only optional parser/context metadata to call payloads when
needed; old call records remain readable. The v2 decoder stays available for
plan/pivot and the existing review compatibility case. The v3 decoder remains
the only path that can satisfy current v3 draft/review opinion provenance.
Do not relabel v2 raw output as v3 or synthesize missing IDs, SHA values, or
aliases.

The reparse adapter selects a decoder from persisted response schema,
consultation type, parser metadata, and captured context. It supports historical
records when those inputs are verifiable. If old data lacks sufficient context,
it returns `legacy_context_unavailable`; it never consults the provider or
re-fetches mutable evidence to guess the original request. Reparse appends a
new `operator-advisor-call.v1` observation with the new parser source version,
the old output SHA, and original-observation link; it does not replace history.

Rollback is code-only: composition returns to the old tool/parser dispatch;
no data downgrade or rewrite is needed. Previously appended reparse records
remain immutable and distinguishable by parser version. Keep both dispatch
paths until the offline v2/v3 corpus and opinion provenance oracles pass.

## 11. Deletion test

Delete `AdvisorConsultationModule` in the design thought experiment. If it is
earning its seam, variant/schema switches, alias identity, status
classification, reparse-context checks, and unit provenance reappear in both
`InvestigationAdvisorTool` and `ReparsePersistedAdvisorOutputTool`; changing an
alias or response variant then requires coordinated edits across consultation,
batching, reparse, and opinion call metadata. That is duplicated complexity
returning to multiple callers, so the module has depth and provides leverage
and locality.

Delete only the current thin aliases wrapper or add a generic handler registry
instead: complexity does not disappear, and callers still need type dispatch,
input contracts, provenance rules, and compatibility. Those would be shallow
interfaces and are not proposed. Keep the dossier and S3 modules because each
already owns a separate behavior set and has real consumers.

## 12. Before/after comparison with verifiable evidence

| Concern | Before — verified | After — proposed |
|---|---|---|
| Preparation | Validation, projection, redaction, dossier, aliases, prompt, hash and preflight are distributed inside the consultation tool (`investigation_advisor_tools.py:164-778`). | One `prepare` contract returns exact prompt bytes/hash, source refs, manifest, response schema and reparse context. Policy remains outside and S3 rechecks it. |
| Variants | Seven request types; five typed v3 types and v2 legacy path (`:78-83,179-187,4689-4696`). | Same seven explicit tagged variants, contract/version declared per row; new variant requires a real caller and versioned parser contract. |
| Ref identity | `_typed_transmitted_refs`, `_transmitted_refs_for_dossier`, `advisor_wire_aliases`, and parser resolution answer related questions separately (M1-B §2; `investigation_advisor_tools.py:4453-4505,4630-4648`; `advisor_wire.py:90-264`; `advisor_contracts.py:166-346`). | One canonical `SourceRef` index feeds rendering, parse, persisted call, and legacy alias adapter; no dossier reconstruction just to allocate aliases. |
| Batch | Over-size editorial input halves units; child keys, per-batch IDs and cursor live in tool logic (`:247-260,435-478,1203-1356`). | Typed batch plan and per-unit result/provenance with the same deterministic child identity and partial/consumption behavior. |
| Parse/reparse | Main parser and reparse each dispatch their own behavior; reparse reads verified stored output and appends an observation without S3 (`:3414-3787,4745-5105`). `suricata_draft` is absent from reparse admission (`:3442-3487`). | Shared version dispatch for live parse and reparse; stored v2/v3 schema/context selected explicitly; Suricata added only with its verified persisted context. |
| Opinion provenance | Service independently reads call observations and verifies v3 output, Bridge invocation, text/output hashes and unit review (`analyst_opinion_service.py:917-1127`). | No opinion-service replacement: parser output preserves the exact identifiers and hashes that service already validates; v2 cannot masquerade as v3. |

## 13. Risks, UNKNOWNs, and limits

- **VERIFIED, highest risk:** current reparse admits `yara_draft`, `draft_text`,
  `review_text`, and `figure_plan`, but not `suricata_draft`, even though
  reparse context construction has Suricata data (`investigation_advisor_tools.py:
  3442-3487,4188-4225`). Extending admission without verifying exact stored
  network facts, reference aliases, lengths, and hashes could parse against
  guessed context. The design therefore gates it on that context.
- **UNKNOWN:** which historical `operator-advisor-call.v1` blobs retain a
  complete v2 parser context, available-tool schema, and alias map, and whether
  their exact raw output blobs still resolve. No DB/blob inventory was allowed
  or performed. Legacy v2 reparse is supported only when the persisted data
  proves those inputs; otherwise the only safe outcome is a reasoned failure.
- **VERIFIED:** `AnalystOpinionService` reads advisor-call payloads with a
  160 KiB maximum and currently requires v3 output for draft/review provenance
  (`analyst_opinion_service.py:917-1010,1029-1127`). **UNKNOWN:** whether large
  normalized editorial call payloads can exceed that threshold in real stored
  cases. Preserve the limit and provenance checks in this design; measure with
  offline fixtures before changing that contract.
- **INFERENCE:** alias allocation can be unified without rewriting historical
  aliases if new `SourceRef` values retain a legacy alias map from the stored
  parse context. Historical context lacking that map remains the principal
  limit on exact replay.
- P10 `invariant_proposals` has a separate refusal path and is explicitly out
  of scope. The consultation module must not absorb that sibling path or alter
  its parser, registry handoff, or refusal behavior.
- The design does not make model output canonical, approve an opinion, execute
  a rule, relax G2/G3/TLP, change S3 idempotency/consume/revocation semantics,
  or create a generic extension framework. The opinion service, canonical
  PostgreSQL records, versioned artifacts, and MinIO hashes remain authoritative.

