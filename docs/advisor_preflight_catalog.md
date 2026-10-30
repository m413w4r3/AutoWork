# Bridge advisor preflight and validation catalog

Updated 2026-10-30 against the advisor implementation in this branch. This is
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

| Type | Automatically assembled material | Only content-availability refusal |
|---|---|---|
| `plan_investigation` | Relevant claims and passages, observations, findings, associated samples, and active hypotheses | No useful canonical claim, observation, finding, or measured fact for the question. An existing hypothesis is optional context. |
| `pivot_strategy` | The planning dossier plus verified measurements available for comparison | No useful canonical claim, observation, finding, or measured fact for the question. Existing hypotheses and measurements are included when available; neither is an operator-supplied prerequisite. |
| `yara_draft` | Verified sample measurements with sample SHA, byte offset, value, and source observation SHA; associated analysis, strings, findings, and candidate rule metadata where available | No verified measurement. The refusal names `sample.analyze` and the associated sample SHA when available. A candidate rule is optional. |
| `suricata_draft` | Measured network facts from associated capture observations, plus relevant observations/findings | No measured network fact. The refusal names `network.capture.inspect` on the associated PCAP. |
| `figure_plan` | Relevant article passages, observations, and current findings | No useful canonical claim, observation, finding, or measured fact for the question. `finding_refs` may be omitted to select current findings automatically. |
| `draft_text` | Relevant article passages, observations, and findings | No canonical claim, observation, or finding body. Draft assertions and proof links are optional advisory annotations. |
| `review_text` | Relevant article passages, observations, findings, and a selected opinion if one was requested | No useful claim, observation, finding, or review-target body. A general question-only review is valid; an explicitly selected opinion still must resolve to its exact current version and valid target. |

An incomplete case is allowed when it contains useful material for the requested
type. Missing contradiction findings are reported as informational, and do not
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

| Surface | Current outcomes | Meaning and operator action |
|---|---|---|
| Tool arguments (`investigation_advisor_tools.py::execute`) | `consultation_type_required`, `consultation_type_unsupported`, `question_invalid`, `evidence_refs_invalid`, `evidence_ref_invalid`, `sample_hash_invalid`, `editorial_units_not_supported`, `editorial_units_out_of_bounds` | Malformed invocation. Send a supported type, a string question, UUID evidence references, a lowercase 64-character SHA-256 selector, and units only for editorial types. Question and reference fields may be omitted where optional. |
| Optional sample selector | `sample_hash_not_associated`, `sample_hash_unavailable`, `advisor_disclosure_restricted` | A selected hash must belong to this investigation and have a canonical sample record/blob. The message names sample intake / `sample.analyze` or explains the TLP, external-LLM, or licence restriction. |
| Explicit evidence reference | `evidence_reference_not_found`, `advisor_evidence_reference_mismatch` | A cited observation must exist in this investigation and match its canonical SHA. Remove or replace an absent/foreign reference, or repair a stale canonical finding link. Automatic context is still assembled when references are omitted. |
| Figure selection | `figure_finding_refs_invalid`, `figure_finding_not_found` | If supplied, finding IDs must be unique UUIDs and current in this investigation. Omit `finding_refs` to use the latest available findings. |
| Exact editorial target | `opinion_not_found`, `opinion_reference_invalid`, `opinion_version_invalid`, `opinion_review_targets_unavailable`, `editorial_unit_invalid`, `editorial_unit_target_invalid`, `editorial_unit_target_duplicate`, `opinion_target_invalid` | These checks apply only when an opinion/version/target was explicitly selected. Omit target fields to request general review or prose. Valid review targets are returned with the refusal when available. |
| Optional selected measurements | `measurements_invalid` (including `measurement_shape_invalid`, `measurement_value_invalid`, `measurement_duplicate`, `measurement_not_verified`, `content_pattern_id_invalid`, `content_pattern_not_verified`) | Any explicitly selected measurements must resolve to unique verified canonical measurements. Omit the `measurements` selector to use automatically assembled verified measurements. |
| Article claim in question | `article_claim_not_found` or an `article` missing-reference preflight entry | A named claim ID must exist in the current article snapshot. The message reports valid claim IDs; refresh the article snapshot or correct the requested ID. |
| Evidence dossier preflight | `advisor_preflight_insufficient_evidence`, `advisor_preflight_needs_retrieval`, `advisor_preflight_technical_blocked`; legacy reason text `required_canonical_evidence_not_in_payload` | These statuses now represent only genuinely missing type-relevant material, an explicitly requested canonical source whose body must be retrieved, or failed source verification. When no useful context exists, `next_action` gives the exact local tool to run. If an explicitly cited claim/source is absent, correct or refresh that reference. |
| Context sizing / reservation | `advisor_context_reservation_mismatch`, `advisor_required_input_truncated` | Exact prompt bytes and tokens must match the approved reservation; changed or truncated required source content is not submitted. There is no per-sample fragment cap. These are accounting/integrity failures, not evidence prerequisites. |
| G2 / external disclosure | `POLICY_DENIED`, `advisor_disclosure_restricted`, `advisor_admission_revoked`, `advisor_admission_recheck_unavailable` | The investigation must authorize Bridge, satisfy TLP and `external_llm_allowed`, and remain approved at pre-send checks. Selected restricted samples and operational secrets are blocked before transport. A failed current admission check requires a fresh approved investigation envelope. |
| Secret preflight | `advisor_secret_field_remaining` | Operational credentials, passwords, signed URLs, or other protected secret fields must be redacted before consultation. Clear the operational secret from the canonical source and retry; evidence content is not a secret-handling instruction. |
| Source/provenance binding | `advisor_evidence_reference_mismatch`, external-effect `source_references_required`, `malformed_source_reference`, `sample_source_missing`, `sample_source_restricted` | The outbound payload must bind to current canonical source IDs, versions, and SHA-256 references. Auto-assembled dossier references supply this binding when available; a missing or changed reference remains a hard provenance refusal. |
| Unexpected canonical context shape | `advisor_context_invalid`, `advisor_context_unavailable` | Stored source data could not be assembled or validated. The refusal identifies source refresh/repair as the next action; it does not label a valid question as an invalid advisor request. |
| Bridge and persistence | `advisor_timeout_or_ambiguous`, `advisor_rate_limited`, `advisor_unavailable`, `advisor_output_invalid`, `advisor_prompt_persistence_failed`, `advisor_output_persistence_uncertain`, `advisor_result_persistence_uncertain` | Transport/provider or archival outcomes after local preflight. These do not indicate missing operator assertions. Ambiguous submission outcomes must not be retried as if no request was sent. |

The input parser also validates claim references embedded in prose, exact source
identity and SHA joins, uniqueness of selected references, and schema types.
Malformed optional draft assertions are ignored for consultative prose; a
well-formed assertion that fails canonical-link validation is likewise not a
reason to reject the prose request. Invalid top-level invocation shapes still
fail schema validation.

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
| `required_canonical_evidence_not_in_payload` | Generic reason text, not a distinct user requirement. It now accompanies only missing required canonical content or provenance. |
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
