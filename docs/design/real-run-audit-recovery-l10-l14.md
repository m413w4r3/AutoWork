# Real-run audit — correction slices L10–L14

Source: audit of the real run of 2026-10-03 (subject 0cb54e11, Bitcoin OP_RETURN / MOIS),
read from `var/diagnostics`, Postgres `model_runs`, MinIO blobs and the rendered PDF.
Complements `real-run-cti-quality-recovery-plan.md`; where they differ (RÉFÉRENCES), this
document wins (decision of the owner, 2026-10-04).

Execution rules: one write-capable worker at a time; the orchestrator reviews every diff,
runs the narrow tests, fixes or sends back, then commits before the next slice. Validation
through `make` targets, Python 3.12 via `uv`, never ad hoc pip. No real bridge call.

## L10 — Bridge wait budgets (P0)

Evidence: Discovery stopped waiting at 899 s (`model_background_wait_budget_exceeded`)
while the bridge was still `running`; the run fell into `needs_review` and needed a manual
import. The budget (`MODEL_BACKGROUND_WAIT_TIMEOUT_SECONDS=900`, `model_gateway.py` resume
path) was added by L2a (`749cec7`); before it, background resume had no cap. The final
poll log line says `bridge_state=completed` even when `resume()` returned the budget-exceeded
state (`discovery/recovery.py`). A synthesis call hung 900 s on the bridge, was released at
+60 min, and no automatic re-emission followed; a manual retry succeeded in 93 s.

Work:
1. Background research/discovery: no hard total cap while the bridge reports `running`
   with progress; keep `WAITING_BACKGROUND` and reschedule. Fail/review only on explicit
   bridge failure or a stall (no progress for a configurable idle window). Budgets become
   per routing hint/role in config and compose; defaults sized for research (>= 90 min total
   ceiling as a safety net, idle window ~20 min).
2. Synchronous drafting stages (synthesis, enrichment, relevance, extraction): shorter
   budget (~300 s) instead of 900 s.
3. After a verified `released`/`failed` reconciliation outcome with `verified_no_answer`,
   verify what the code does today; if no automatic bounded re-emission happens, add it
   (max 2, existing L2a contract). Do not re-emit when the state is unknown.
4. Fix the misleading `bridge_state=completed` observation.
5. `composer Temporary Chat introuvable` (`bridge_ui_timeout`): longer back-off between job
   retries and an explicit diagnostic; no bridge DOM change (external repo).

Acceptance: fake bridge `running` beyond the old budget then `completed` -> run succeeds,
no `needs_review`; stalled bridge -> review with a stall code; synthesis hang -> bounded
retry; tests in `backend/tests`; `make test-backend lint-backend typecheck-backend` green.

## L11 — Publication layout and wording (P0)

Evidence (PDF): RÉFÉRENCES contains sub-titles "Chronologie" and "Sources complémentaires";
9 identical footnote URLs; off-subject timeline (Necurs 2013, Glupteba 2019, Chinese LLMs);
header/footer show `Bulletin-CODE`, `infrastructures X`, `Bulletin n°XX`; the synthesis
says "sur la seule base du pack fourni" / "les éléments fournis".

Work:
1. Single RÉFÉRENCES part: dated entries (as in the previous bulletin), no "Chronologie" or
   "Sources complémentaires" sub-title; one footnote/reference per distinct source, reused
   by every entry citing it. Apply to `chpTypst/RENDERER/publication_helpers.typ` and
   `frontend/src/components/ProductionArtifactView.tsx` (same order and wording).
2. Timeline: only DIRECT (and corroborating) events; exclude CONTEXT
   (`build_synthesis_timeline`, `production_synthesis.py`). Bump the timeline policy version.
3. Template header/footer placeholders replaced from edition data (country, month, number or
   removed when unknown): `chpTypst/UTILS/header_footer.typ`, `chpTypst/main.typ`.
4. Synthesis prompt + QA: forbid pipeline vocabulary ("pack", "éléments fournis",
   "base du pack"); QA check fails publication with a code, not silently.
5. Update `real-run-cti-quality-recovery-plan.md` §4 wording on RÉFÉRENCES.

Acceptance: real-Typst render test (TMPDIR under $HOME) shows one RÉFÉRENCES part, no
placeholder, deduplicated notes; frontend test updated; backend + frontend suites green.

## L12 — Editorial enrichment contract (P0)

Evidence: 6 blocks rejected, 0 enrichments, stage `success`. Causes: prompt says
`infection_chain` as relation while the parser accepts `factual|inference|comparison` and
the wire spec never lists them; `NODE N001` and `NEEDS N001` share the local-id namespace;
`PURPOSE_EVIDENCE` must be a subset of row/node evidence (one extra handle drops a valid
table); `source_figure_inventory_contains_unresolved_items` counts deliberately excluded
duplicates as unresolved.

Work:
1. Spell out `RELATION_TYPE: factual | inference | comparison` in spec and example; remove
   `infection_chain` from relation vocabulary in the prompt (diagram KIND keeps it).
2. Tolerant normalisation: `RELATION_TYPE: infection_chain` on a diagram of that kind maps to
   `factual`, still validated against evidence.
3. Separate id namespaces per block kind/parent (NODE/COLUMN/ROW scoped to their block;
   NEEDS/FIGURE/TABLE/DIAGRAM/ANNOTATION top-level).
4. Purpose evidence: reduce to the handles actually carried, with a warning, when the
   intersection is non-empty; reject only when empty.
5. One bounded targeted repair call for rejected blocks that have valid siblings, reusing the
   L9 revision machinery; versioned and idempotent.
6. If the model proposed something and every proposal ended rejected, the stage ends
   `needs_review` (`editorial_enrichment_empty_after_rejections`), not `success`.
7. Warning only for figures still pending collection/selection, not EXCLUDED_BY_RULE.

Acceptance: replay of the raw output of 2026-10-03 (fixture, anonymised) yields the table and
the diagram; unit tests per rule; suites green.

## L13 — Semantic typography coverage (P1)

Evidence: 7 annotations, almost all in the lead; none in the timeline; Namecoin, Necurs,
Glupteba, EtherHiding, Chainalysis, Bitcoin, Ethereum unannotated.
`semantic_entities_from_extraction` uses `fact.value` (a sentence); `annotate_paragraph`
ignores model proposals outside their anchor paragraph.

Work:
1. Extraction contract gains typed entities (name, role, aliases) per fact/event, version
   bump, parser tolerant, never invented; legacy artifacts remain readable.
2. `semantic_entities_from_extraction` feeds names, not sentences.
3. Model annotation proposals become a document-level lexicon applied to every paragraph
   (timeline, sections, lead), exact-match, no character change, existing priority rules.
4. Dedicated annotation call separate from table/diagram proposals (short, anchored list).
5. QA check on coverage: every extracted entity present in the text is annotated at each
   occurrence; warning level first, failure when below threshold.

Acceptance: fixture of this run -> Necurs, Glupteba, Namecoin, EtherHiding, Chainalysis,
Bitcoin OP_RETURN annotated at all occurrences incl. references; annotation never alters text.

## L14 — Counter-analysis and substance gate (P1/P2)

Evidence: Bitquery (`counter_analysis`) never reaches the synthesis; classifier emitted no
source-pair blocks, labelled Bitquery items OUT_OF_SCOPE/INDETERMINATE, and 8 proposals were
rejected for `invalid_reason_for_classification` (e.g. OUT_OF_SCOPE + `context_source_without_relation`).
The subject rests on one paragraph of a multi-actor article (5 direct facts, 20 context).

Work:
1. Align classifier prompt and validator on valid classification x reason pairs; list the
   pairs in the wire spec; tolerant repair of an invalid pair when the classification is
   unambiguous.
2. For a source with editorial role `counter_analysis`, require the classifier to evaluate
   source-pair relations against the primary claims; counter-indications are carried into the
   synthesis reserve context and must be usable in prose.
3. Substance gate before synthesis: configurable minimum of DIRECT facts/events; below it the
   run stops in `needs_review` with an explicit reason instead of producing generic prose.

Acceptance: replay of this run -> Bitquery reserve present in the synthesis input pack and
`source_pair_relation_count > 0`; gate unit tests; suites green.

## Dependencies

L10, L11, L12 are independent; run in this order (P0 first). L13 after L12 (shared
enrichment/annotation code). L14 last (touches extraction/relevance contracts after L13).
