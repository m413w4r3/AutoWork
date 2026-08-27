# M3 / P10 — proposal conversation contract

Status: locked pre-implementation contract. P10 consumes this file; it does not redesign it.

## Scope

P10 lets a model propose pivots from the already-filtered P09 invariant registry. The model proposes; AutoWork validates, records, and keeps analyst authority. P10 does not execute a VirusTotal query, acquire a sample, promote a corpus member, accept an invariant, or mutate production artifacts. It adds no public endpoint unless the P10 prompt explicitly says otherwise; operational wiring remains for later milestones.

No new model SDK or external API contract is introduced. Reuse the existing `ModelConversationService` / `ModelGateway` stack.

## Conversation lifecycle

A proposal conversation is investigation-scoped and uses `ConversationPurpose.PIVOT_RESEARCH`.

- Prefer the investigation's persisted `pivot_conversation_id` when present.
- For a new conversation, create it through `ModelConversationService` and persist its id onto the investigation using the existing investigation repository/UoW boundary.
- For `CHATGPT_BRIDGE`, use `CONTINUE` only when the existing conversation has a persisted head turn with a verified `external_turn_id`; otherwise use `FRESH`.
- For `APPLICATION_MANAGED` transports (local Qwen), every proposal turn is `FRESH`; the current conversation service intentionally does not allow `CONTINUE` there.
- Never bypass `ModelConversationService` by calling a provider/bridge client directly.

Each turn uses a deterministic idempotency key derived from investigation id + cycle number + canonical P09 registry input hash + prompt version. Retrying an identical turn must not submit twice.

## Policy before any external model call

The exact sample set supplied to the proposal builder is the investigation sample set. Derive aggregate policy from those sample records using the canonical `derived_policy(...)` function at every turn; do not cache a weaker decision.

- If `external_llm_allowed` is false, an external provider/bridge call is blocked before submission.
- `do_not_submit` and TLP are preserved as policy metadata/provenance and must never be relaxed.
- A local `APPLICATION_MANAGED` Qwen route may be used when configured by the existing gateway; P10 does not redefine provider routing.
- Never include raw binary bytes, secrets, API keys, signed URLs, or unrestricted source-document text in the proposal prompt.

The proposal prompt may contain only the bounded structured investigation context required by the schema: candidate stable ids/types/normalized values/statuses, deterministic support/frequency summaries, persisted occurrence locations/provenance, cycle/budget summary, and previously recorded analyst decisions/rejections needed to avoid repetition.

## Input selection

Only P09 rows with status `CANDIDATE` are eligible for model proposal input. `ACCEPTED` may be shown as prior analyst context but is not re-proposed. Deterministically rejected rows are never offered as viable pivots.

Input ordering is deterministic: feature type, stable id. The builder is bounded by explicit caller/configured maxima for candidate count and serialized prompt bytes. Truncation must be deterministic and recorded in provenance; never silently exceed the cap.

A canonical SHA-256 of the exact structured proposal input is persisted/referenced so the turn can be reproduced without reconstructing mutable state.

## Structured model output

The model response is schema-constrained. Use a strict Pydantic model and reject free-form outputs that do not validate.

Minimal proposal schema:

- `summary`: bounded text, advisory only;
- `proposals`: bounded list;
- each proposal has `candidate_stable_id`, `pivot_kind`, `pivot_value`, `rationale`, optional `priority`.

`candidate_stable_id` must reference an eligible P09 candidate in the exact input snapshot. The model cannot invent a new invariant id.

Allowed `pivot_kind` values are only the query primitives that AutoWork already knows how to represent safely at this point:

- `VT_INTELLIGENCE_SEARCH`
- `VT_FILE_RELATIONSHIP`
- `LOCAL_CORPUS_LOOKUP`

P10 persists proposals but does not execute them. Any additional kind is invalid output.

`pivot_value` is data, never executable syntax. P10 must not concatenate it into shell commands or execute it. For VirusTotal kinds it is stored as a proposed query/identifier for later validated execution.

## Validation and rejection journal

Validation is deterministic after the model returns. A proposal is accepted into the proposal registry only when all of these hold:

1. schema is valid and bounded;
2. referenced `candidate_stable_id` exists in the exact input snapshot and remains eligible;
3. `pivot_kind` is allowed;
4. `pivot_value` is non-empty, within configured length, contains no NUL, and passes the later-execution-safe lexical constraints defined by the P10 domain helper;
5. duplicate proposal identity is not already persisted for the investigation/cycle/input hash;
6. proposal does not contradict an append-only analyst rejection/decision already persisted for the same target when that decision explicitly forbids retry.

Rejected model proposals are inspectable and append-only. Persist at least model run/turn id when available, investigation id, input hash, rejection code, safe bounded reason, referenced candidate id if parseable, and a SHA-256 of the rejected raw output/proposal fragment. Do not persist secrets or unlimited raw model text in the rejection row.

Rejection codes are exactly:

- `INVALID_SCHEMA`
- `UNKNOWN_CANDIDATE`
- `INELIGIBLE_CANDIDATE`
- `UNSUPPORTED_PIVOT_KIND`
- `INVALID_PIVOT_VALUE`
- `DUPLICATE_PROPOSAL`
- `ANALYST_POLICY_REJECTED`

Invalid model output is a model proposal rejection, not an investigation technical failure. A transport/gateway failure remains a technical/model execution failure according to the existing conversation service contract.

## Persistence

P10 creates migration `0012_pivot_proposals.py` with `down_revision = "0011_invariant_registry"` and never edits migrations 0001–0011.

Recommended minimal tables are `pivot_proposal_runs`, `pivot_proposals`, and `pivot_proposal_rejections`; exact decomposition may vary only if the contract is preserved.

Required database guarantees:

- proposal run replay uniqueness from `(investigation_id, cycle_number, input_sha256, prompt_version)`;
- proposal identity uniqueness from a deterministic key over investigation + input + candidate stable id + kind + canonical value;
- rejection replay uniqueness from a deterministic rejection key;
- proposal/rejection search by investigation and cycle;
- no UPDATE/DELETE path for append-only rejections;
- store foreign keys to model conversation/turn/run where the existing schema makes them available without coupling to provider internals.

P10 must not increment pivot budget merely for asking the model to propose. `PIVOT_RUNS` is consumed by the later actual pivot execution, not by proposal generation.

## Model-output authority boundary

A persisted proposal is not an analyst decision, not an accepted invariant, and not evidence. It is advisory model output with full provenance.

The model cannot:

- change investigation state to completed/exhausted;
- accept/reject a P09 invariant as analyst authority;
- execute a pivot;
- acquire or validate a sample;
- promote a reference corpus member;
- change TLP/do-not-submit/external-LLM policy;
- mutate the verified SYNTHESIS or analyst input pack.

## P10 tests to lock

Tests must cover: external policy blocked before gateway submission; local application-managed turns use `FRESH`; bridge `CONTINUE` only with verified persisted head; deterministic idempotency replay; exact candidate snapshot validation; every rejection code; duplicate persistence under race/replay; invalid output does not fail the investigation; no pivot budget consumption; no execution side effect; and safe bounded rejection persistence.
