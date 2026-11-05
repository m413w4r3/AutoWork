# Bridge advisor preflight and validation catalog

Updated 2026-11-04 against the advisor implementation in this branch. This is
a code-path catalog, not an authorization or a substitute for the current G2,
TLP, provenance, revocation, and external-effect guards. The operator may call
`advisor.consult` with a consultation type and question alone. Optional source
selectors prioritize canonical context; the server assembles the dossier from
the current investigation snapshot.

The seven supported consultation types are `plan_investigation`,
`pivot_strategy`, `yara_draft`, `suricata_draft`, `figure_plan`, `draft_text`,
and `review_text`. Completeness is implemented in
`backend/src/cti_app/application/advisor_dossier.py`; there is no separate
`advisor_completeness*` module in the current tree.

## Automatic dossier assembly

`InvestigationAdvisorTool.execute` and `_consultation_data` assemble current
article passages, investigation observations, associated sample context,
verified sample measurements and network facts, current findings, candidate
rules, and (for planning and pivot work) current hypotheses. `figure_plan`
loads current findings when no IDs are supplied. The selected sample hash, if
provided, moves matching evidence to the front of the dossier. Explicit
evidence references are merged with automatically loaded investigation
observations; an omitted reference list does not mean an empty dossier.

Sample and observation content remains subject to canonical association,
blob-read and SHA-256 verification, TLP bounds, `external_llm_allowed`,
licence restrictions, and operational-secret redaction. Restricted evidence is
excluded automatically when it is unrelated to an explicit target; selecting
restricted evidence or a finding linked to it is refused. Complete verified
sample bytes are read and included without a per-sample fragment cap or prompt
shortening. The exact encoded prompt cost is still reserved against the
approved investigation context budget, and provider transport has its own
capabilities.

## Requirements by consultation type

| Type | Automatically assembled material | Incomplete-context warning | Blocking condition |
|---|---|---|---|
| `plan_investigation` | Relevant claims and passages, observations, findings, associated samples, and active hypotheses | Missing or omitted proof is listed in the dossier with the available source and a suggested retrieval action. | No consultation question/task, approved G2 envelope or budget, verified canonical source binding, or usable transport. |
| `pivot_strategy` | The planning dossier plus verified measurements available for comparison | Missing observations or measurements are listed with an action such as `sample.analyze` when an associated sample is known. | Same technical, G2, and transport blockers as other types. |
| `yara_draft` | Verified sample measurements with sample SHA, byte offset, value, and source observation SHA; associated analysis, strings, findings, and candidate rule metadata where available | No verified measurement is a warning. Bridge receives available context and a suggested `sample.analyze` action. | Same technical, G2, and transport blockers as other types. |
| `suricata_draft` | Measured network facts from associated capture observations, plus relevant observations/findings | No measured network fact is a warning; the dossier suggests `network.capture.inspect` when a PCAP is available. | Same technical, G2, and transport blockers as other types. |
| `figure_plan` | Relevant article passages, observations, and current findings | Missing or invalid optional `finding_refs` are omitted and current findings are selected automatically where possible. | Same technical, G2, and transport blockers as other types. |
| `draft_text` | Relevant article passages, observations, and findings | Invalid assertions, units, anchors, or proof links are dropped with a warning while available text/context is sent. | A blank explicit question with no text target, or no valid source binding when the contract requires one. |
| `review_text` | Relevant article passages, observations, findings, and a selected opinion if one was requested | Invalid optional opinion/version/unit/anchor selectors are dropped; the current canonical opinion and its available units are used when present. | A blank explicit question with no reviewable target, unavailable canonical text, or no valid source binding when required. |

An incomplete case is allowed when it contains useful material for the requested
type. The dossier includes a short “Contexte indisponible / omis” section when
optional inputs or requested proof could not be loaded. The tool result exposes
the same omissions through `input_warnings`, `omissions`, and
`next_action_hint`. Missing contradiction findings are informational, and do not
block a contradiction search. `do_not_submit` is not a Bridge disclosure veto;
the server enforces the applicable TLP, external-LLM, licence, and G2 rules.

## Post-call response validation

These checks run after the fake/real Bridge transport has been called. They
classify untrusted output; they are not evidence prerequisites for asking the
question.

| Type | Response checks and effect |
|---|---|
| `plan_investigation`, `pivot_strategy` | `advisor_semantic_parser.py` validates response status, recommendation shapes, and source aliases. Invalid, ambiguous, or refused provider content is recorded as such after submission; it does not change preflight eligibility. |
| `yara_draft` | `advisor_contracts.py` accepts prose with no rule block. A supplied block is checked for its YARA rule header, motifs, invariants, and source aliases. Missing/invalid structure becomes a warning or partial/unusable draft, not a preflight refusal. |
| `suricata_draft` | Prose can be returned alone. A supplied block is checked for its Suricata header, measured facts, and source aliases; parser warnings stay on the advisory result. |
| `figure_plan` | Prose can be returned alone. A supplied figure structure is checked for title, legend, node/edge shape, endpoints, direction evidence, and source aliases. Invalid structure is a parser result/warning after Bridge. |
| `draft_text` | Free prose is normalized as consultative text. If structured draft blocks are supplied, unit targets, sentences, assertions, and references are validated; missing assertion/reference annotations do not invalidate the prose request. |
| `review_text` | General prose is normalized without a selected opinion. If an exact review target was supplied, its version and target are checked before the call; response remarks and references are normalized afterward. |

Shared post-call terminal reasons include `response_invalid`,
`response_status_invalid`, `advisor_refused`, `advisor_marked_invalid`,
`advisor_marked_ambiguous`, `duplicate_response_key`,
`consultation_type_mismatch`, `evidence_context_invalid`, and
`operational_secret_detected`. Alias and typed artifact warnings such as
`reference_alias_unknown`, `reference_alias_invalid`,
`yara_rule_header_missing`, `suricata_rule_header_missing`,
`figure_plan_invalid`, `draft_sentence_reference_invalid`, and
`review_refs_invalid` describe provider output. They do not require the operator
to pre-fill canonical assertions just to receive a consultative answer.

## Shared refusal and validation surfaces

| Surface | Bloquant | Avertissement et action opérateur |
|---|---|---|
| Tool arguments (`investigation_advisor_tools.py::execute`) | `consultation_type_required`, `consultation_type_unsupported`, non-string question, or explicitly blank question with no text target | Malformed optional selectors and unresolved references are dropped; a missing question field can use the consultation type's default task. |
| Optional sample/evidence selectors | G2/TLP/licence restrictions, revoked sample association at the pre-send recheck, or cross-investigation data that would otherwise be disclosed | `sample_hash_not_associated`, `sample_hash_unavailable`, malformed/unknown evidence UUIDs, and unavailable optional references are omitted and reported. Bridge receives the rest of the dossier. |
| Figure and measurement selections | Canonical source integrity failure when selected material cannot be verified | Invalid, duplicate, stale, or unavailable `finding_refs` and measurement selectors are dropped; current verified material is selected when available. |
| Editorial assertions and targets | No text remains to process, or canonical text/source identity is corrupt | Invalid draft assertions, review units, versions, forms, anchors, and optional proof links are dropped with warnings. A current opinion or valid text target is used when available. |
| Article claims and explicit proof | Canonical SHA/ID mismatch or an unverifiable required source identity | `article_claim_not_found`, `NEEDS_RETRIEVAL`, `INSUFFICIENT_EVIDENCE`, and `required_canonical_evidence_not_in_payload` are warnings. The dossier names the missing context and suggested local tool/action. |
| Evidence preflight statuses | `TECHNICAL_BLOCKED` only when required canonical metadata/source verification fails; `POLICY_BLOCKED` for an active disclosure/admission boundary | `READY` means the preflight has no completeness warning. `NEEDS_RETRIEVAL` and `INSUFFICIENT_EVIDENCE` continue to Bridge with available context, dossier omissions, and `next_action_hint`. |
| Context sizing / reservation | `advisor_context_reservation_mismatch`, budget exhaustion, `advisor_required_input_truncated` | Exact prompt bytes and tokens must match the approved reservation. There is no per-sample fragment cap. |
| G2 / external disclosure | `POLICY_DENIED`, `advisor_disclosure_restricted`, `advisor_admission_revoked`, `advisor_admission_recheck_unavailable`, TLP or licence restriction | `do_not_submit` is handled only where its effect contract requires it. Selected restricted samples and operational secrets remain blocked before transport. |
| Secret preflight | `advisor_secret_field_remaining` | A safe redaction or omission is reported in the dossier and tool warnings; credentials, passwords, and signed URLs never enter the Bridge prompt. |
| Source/provenance binding | `source_references_required`, malformed canonical identity/SHA, or changed canonical source bytes | `advisor_evidence_reference_mismatch` for an optional source-document link is a warning when the verified article snapshot remains bound and is sent. An unresolved canonical binding remains blocked. |
| Unexpected canonical context shape | `advisor_context_invalid`, `advisor_context_unavailable`, storage/read verification failure | The affected source is not represented as verified; the tool reports the repair/retrieval action where available. |
| Bridge and persistence | `advisor_timeout_or_ambiguous`, `advisor_rate_limited`, `advisor_unavailable`, output or archival uncertainty | These are transport/provider or archival outcomes. Ambiguous submission outcomes must not be retried as if no request was sent. |

The input parser still validates exact source identity and SHA joins, disclosure
policy, and schema types. When a local check concerns an optional selection,
the selection is omitted and Bridge receives the question with available context.
Canonical integrity failures and invalid top-level invocation shapes remain
blocking.

## Advisory text and canonical promotion are separate gates

`draft_text`, `review_text`, `yara_draft`, `suricata_draft`, `figure_plan`,
`pivot_strategy`, and `plan_investigation` return untrusted advisory material.
No assertion list, proof-link list, or operator-authored claim is needed merely
to ask Bridge for prose or a draft. Source references, exact artifact versions,
canonical assertions, sample qualification, and human G3 approval continue to
be enforced when the result is promoted into a canonical finding, rule, article
opinion, publication, or other durable object. These promotion and external
effect contracts have not been weakened by this catalog.

## Previously observed codes and where they belong

| Observed code / message | Current interpretation |
|---|---|
| `advisor_input_invalid` with `no_verified_measurements` | A YARA no-measurement friction. It is replaced by the type-specific useful-material preflight and actionable `sample.analyze <sha>` guidance. `advisor_input_invalid` remains the wrapper for genuinely malformed invocation/provenance inputs. |
| `required_canonical_evidence_not_in_payload` | Legacy diagnostic text for an incomplete dossier. It is surfaced as an omission warning and does not prevent dispatch when another valid source binding and technical prerequisites are available. |
| `assertions_required` | Not a consultation prerequisite. Assertion checks are optional advisory annotations; promotion contracts remain strict. |
| `source_references_required` | External-effect consume guard for a request with no canonical source references. The dossier now supplies references from assembled content; if none can be resolved, this remains a provenance refusal. |
| `human_qualification_required` | Qualification/promotion endpoint guard, not a Bridge consultation preflight. |
| `no_verified_context_bytes` | Research/context-building guard outside `advisor.consult`; it is not an advisor consultation requirement. |
| `advisor_input_rejected` | Removed for the known missing/foreign evidence-reference path and for unexpected dossier `KeyError`/`TypeError`/`ValueError`; the first has a specific provenance code and the latter is an actionable canonical-context failure. |

## Guard coverage limitation

The advisor checks selected sample restrictions against the current envelope
while assembling the dossier and rechecks G2 admission before transport. The
separate database consume guard in
`backend/src/cti_app/infrastructure/database/repositories/external_effects.py`
rechecks sample subject, TLP RED, `external_llm_allowed`, `do_not_submit` where
the effect contract uses it, and licence restrictions while locking sample
rows. It does not currently compare non-RED sample TLP values against the
envelope's approved TLP ceiling at consume time. That check is outside the
advisor-owned module set and remains an independent follow-up for the parent.

## Deterministic offline oracle

`backend/tests/test_advisor_chain_minimal_calls.py` seeds 15 synthetic
associated samples, static FLOSS/capa/ILSpy observations, VirusTotal report
observations, current findings, two article claim passages, no candidate rules,
and asks all seven types with question-only calls to a fake Bridge transport.
It asserts that the provider receives assembled context, prose remains
consultative, optional source links do not block, and restricted sample,
revoked G2, and non-associated hash cases remain refused. It also checks a
3.2 MB associated sample is sent completely and charged at its exact encoded
prompt size. It makes no provider, PostgreSQL, or Docker calls.
