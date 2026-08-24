Benchmark spec: R67a (unchanged) — refacto_baseLine/R67_benchmark_spec.md
Baseline: 2c7a15d1bfe905554f64d560689c5f9e111fc400
Candidate (R68): bd4e4c4653b10487e1d054c72aa2733e124bd6a9
ctx.py used on both snapshots: bd4e4c4653b10487e1d054c72aa2733e124bd6a9 (R68's ctx.py, field-aware lexical ranking)

Engine mode for all 24 queries: `--lexical-only`, `-k 8`, default per-file / relative floor / lexical-meta weights, no embeddings, no `.env`, no `--path`. Fresh index built per snapshot (`build --lexical-only`) with the R68 ctx.py extracted to a tool dir and run from each worktree's cwd. Both worktrees' `status` reported `source_index: current`, `dense_embeddings: incomplete` (expected — lexical-only).

R68 changed only `scripts/ctx/ctx.py` (ranking) and `scripts/ctx/tests/test_ctx.py`. No application code differs between this replay's "candidate" snapshot and R67's frozen "final" snapshot (882c8a6) — the two are identical outside `scripts/ctx/` and `refacto_baseLine/`. `minimal_owner_files` ground truth is therefore reused from `R67_ab_results.md` wherever the same owner file was independently reconfirmed present in this replay's results; where R68's ranking surfaced a different (but independently verified, per the frozen relevance rule) file, that is documented per-query below as a revised ground-truth call, not a rank-only artifact.

---

## Résultats — tableau

| Q | base rank | cand rank | base top3 | cand top3 | base owner | cand owner | base files-before-hit | cand files-before-hit | base lines-before-hit | cand lines-before-hit |
|---|---|---|---|---|---|---|---|---|---|---|
| Q1 | 1 | 1 | PASS | PASS | 1 | 1 | 1 | 1 | 30 | 30 |
| Q2 | 1 | 1 | PASS | PASS | 1 | 1 | 1 | 1 | 33 | 33 |
| Q3 | 1 | 1 | PASS | PASS | 2 | 3 | 1 | 1 | 23 | 23 |
| Q4 | 1 | 1 | PASS | PASS | 1 | 2 | 1 | 1 | 106 | 54 |
| Q5 | 1 | 1 | PASS | PASS | 1 | 1 | 1 | 1 | 106 | 106 |
| Q6 | 4 | 4 | FAIL | FAIL | 1 | 1 | 3 | 3 | 126 | 126 |
| Q7 | 1 | 1 | PASS | PASS | 2 | 2 | 1 | 1 | 110 | 5 |
| Q8 | 3 | 3 | PASS | PASS | 2 | 2 | 2 | 2 | 132 | 132 |
| Q9 | 4 | 7 | FAIL | FAIL | 1 | 1 | 3 | 6 | 48 | 279 |
| Q10 | MISS | MISS | FAIL | FAIL | 1 | 1 | 2 | 2 | 76 | 76 |
| Q11 | 5 | 5 | FAIL | FAIL | 1 | 1 | 3 | 3 | 174 | 180 |
| Q12 | 1 | 1 | PASS | PASS | 2 | 2 | 1 | 1 | 59 | 59 |

---

## Détail par requête

### Q1 — `conversation lifecycle release after discovery amendment production publication`

- Baseline relevant owner: `backend/src/cti_app/domain/model_conversations.py` (`ConversationLifecycle.release`) — rank 1.
- Candidate relevant owner: same path/method, unchanged (`ConversationLifecycle.release`) — rank 1.
- Notes: **Revised ground truth vs R67.** Under the old lexical scoring this domain state-machine method never surfaced in top-8 (R67 used `release_conversation` at rank 6/4 instead). Its body was inspected directly: it is the literal state-machine implementation of "release" (`ConversationLifecycleStatus` transitions, idempotence guard, docstring "Release the conversation with an explicit outcome"). It satisfies rule 1 (direct implementation) more precisely than the bridge route that merely calls into it, and the file is untouched by the chatgpt-bridge split, so it is identical on both snapshots. This is exactly the kind of fix R68 targeted (Q1's owner rank was the first named problem in the task) and it now lands at rank 1 on both sides — a large, generic improvement (rank 6→1 baseline, 4→1 candidate; MISS→1 owner file in the minimal set).

### Q2 — `persist and reload model conversation state database repository`

- Baseline relevant owner: `backend/src/cti_app/infrastructure/database/repositories.py` (`SqlAlchemyModelConversationTurnRepository`) — rank 1.
- Candidate relevant owner: `backend/src/cti_app/infrastructure/database/repositories/model_conversations.py` (same class, split file) — rank 1.
- Notes: This was the second named problem (R67: MISS on both sides "despite the tokens model/conversation/repository"). Field-aware lexical scoring now weights the class-name tokens (`sql`, `alchemy`, `model`, `conversation`, `repository`) at the symbol level instead of diluting them across an undifferentiated term set, and it surfaces at rank 1 on both snapshots — MISS→PASS, exactly the intended fix, with no special-casing of this query or symbol (the same generic `FIELD_WEIGHT_SYMBOL` applies everywhere).

### Q3 — `recover incomplete discovery operation after failed or interrupted model run`

- Baseline relevant owner: `chatgpt-bridge/server.py` (`RunRegistry.recover_interrupted`) — rank 1, unchanged from R67.
- Candidate relevant owner: `chatgpt-bridge/bridge/registry.py` (`RunRegistry.recover_interrupted`) — rank 1, unchanged from R67.
- Notes: Unaffected by the ranking change; `minimal_owner_files` reused from R67 (baseline 2: `server.py` + `application/discovery.py`; candidate 3: `registry.py` + `application/discovery/service.py` + `application/discovery/recovery.py` — the R27 split, per R67's own inspection). `domain/discovery.py::recover_incomplete_source_urls` also appears at rank 3 both sides (unchanged), consistent with R67.

### Q4 — `validate discovery merge conflicts before applying cumulative merge`

- Baseline relevant owner: `backend/src/cti_app/application/discovery_cumulative.py` (`validate_merge_plan`, `_resolve_merge_run`) — rank 1.
- Candidate relevant owner: `backend/src/cti_app/application/discovery/cumulative/validation.py` (`validate_merge_plan`) — rank 1 (now ranks *above* `_resolve_merge_run`, since `validate_merge_plan` is a more literal symbol match for "validate ... merge conflicts").
- Notes: `minimal_owner_files` reused from R67 (1→2, the validation/orchestration split is a structural fact of the refactor, independent of ranking). Rank unchanged at 1 both sides; lines-before-hit dropped (106→54 baseline chunk vs the smaller `validate_merge_plan` chunk now ranked first on the candidate side) — a byproduct of which chunk wins the top slot, not a regression.

### Q5 — `detect stale discovery merge and replan outdated merge run`

- Baseline/candidate relevant owner: `discovery_cumulative.py` / `application/discovery/cumulative/service.py` (`_resolve_merge_run`, the verified stale-detect/replan logic) — rank 1 both sides, unchanged from R67. `minimal_owner_files` reused from R67 (1→1); `errors.py` (candidate) still correctly excluded (holds only the exception class).

### Q6 — `brief amendment repository indexing querying storage retrieval`

- Baseline relevant owner: `backend/src/cti_app/infrastructure/database/repositories.py` (`SqlAlchemyBriefDraftRepository`) — rank 4. Body inspected directly (`append`/`get`/`get_current`/`list_for_subject` against `BriefDraftRow`): a literal repository implementing storage/retrieval.
- Candidate relevant owner: `backend/src/cti_app/infrastructure/database/repositories/briefs.py` (same class, split file) — rank 4.
- Notes: **Regression vs R67.** R67 scored this query PASS (rank 1) via `save_brief_draft` in `backend/src/cti_app/api/production.py`, explicitly flagged as a judgment call ("brief-adjacent, not literally amendment"). Under R68's ranking, `production.py` no longer appears anywhere in the top 8 on either snapshot; `domain/briefs.py::BriefAmendment` now ranks 1 (a plain dataclass — correctly excluded, it doesn't implement storage/retrieval), followed by two excluded test chunks, before `SqlAlchemyBriefDraftRepository` at rank 4. Both candidate readings (R67's `save_brief_draft` and this replay's `SqlAlchemyBriefDraftRepository`) are defensible "adjacent, not literally amendment" judgment calls; under R68 the more literal repository class is favored (field-aware symbol weight pushes `BriefAmendment`'s exact-name match to rank 1 even though it isn't a repository), but the query is now FAIL top3 on both sides where R67 was PASS. This is a genuine, reported regression, not glossed over.

### Q7 — `create edition frontend form submit API persistence`

- Baseline relevant owner: `frontend/src/App.tsx` (`EditionCreatePage`) + `frontend/src/api/editions.ts` (`createEdition`) — rank 1, unchanged from R67.
- Candidate relevant owner: `frontend/src/pages/EditionCreatePage.tsx` (now ranked via its `formValue` helper, a small 5-line chunk) + `editions.ts` — rank 1, unchanged from R67. `minimal_owner_files` reused (2→2). Pass/rank unaffected; lines-before-hit shrank because a smaller chunk within the same file now wins rank 1.

### Q8 — `production workflow orchestrate parse render publish stages`

- Baseline/candidate relevant owner: `backend/src/cti_app/application/production_workflow.py` (`_execute_synthesis_stage` baseline / `_execute_extraction_stage` candidate) — rank 3 both sides (was rank 2 in R67; a frontend test at rank 2 and a non-orchestrating logging helper `_log_parse` at rank 1 are both correctly excluded — `_log_parse` only records diagnostics, it does not orchestrate stages, so it does not satisfy the frozen relevance rule). Still PASS top3 both sides (rank 3 ≤ 3), consistent with R67's verdict for this query. `minimal_owner_files` reused from R67 (2→2, `production_workflow.py` + `production_stages.py`).

### Q9 — `ChatGPT bridge browser extension server request conversation routing`

- Baseline relevant owner: `chatgpt-bridge/server.py` (`Bridge` class, docstring: "routage par `id`" — the actual extension-WebSocket request router) — rank 4.
- Candidate relevant owner: `backend/src/cti_app/integrations/models.py` (`ChatGPTBridgeTransport` — translates and routes requests to the bridge server) — rank 7.
- Notes: **Regression vs R67**, and the clearest example of an unintended side effect of field-aware ranking. R67's owner (`run_generation`, rank 1 both sides) does not appear anywhere in top-8 on either snapshot in this replay. `chatgpt-bridge/AGENTS.md` and `chatgpt-bridge/README.md` now rank 1–2 on both sides: their markdown headings ("ChatGPT Bridge — external conversation lifecycle", "ChatGPT Mini-Bridge") literally contain query tokens, and the markdown chunker treats a heading as the chunk's `symbol` — so `FIELD_WEIGHT_SYMBOL` boosts doc headings exactly like a code symbol, which is a genuine blind spot of this generic change (it doesn't distinguish "symbol of a doc section" from "symbol of a function/class"). AGENTS/README files are excluded from the relevance count per the frozen rule, but their inflated score pushes the relative-floor cutoff up, crowding out lower (but still valid) code hits — this is very likely why `run_generation` fell out of top-8 entirely. `Bridge` (baseline) and `ChatGPTBridgeTransport` (candidate) are independently verified as directly implementing/orchestrating request routing (rule 1/2), so they are counted as the first relevant hits found, but both queries now FAIL top3 where R67 passed at rank 1. Documented as a known limitation, not fixed in this task (see verdict discussion).

### Q10 — `published brief immutability amendment preservation rules`

- Baseline/candidate relevant owner: `backend/src/cti_app/application/amendment_service.py` (unchanged path both sides) — still MISS, not in top 8, same as R67. Both snapshots' top-3 candidates are still a test file (`test_editorial_preservation_integration.py`) and the ADR doc (`0003-editorial-preservation-increment-3.md`), both excluded categories. This is the query the task explicitly warned would "probablement rester FAIL" (Q10 was named as a problem, but a generic fix was forbidden) — confirmed: no special-casing was added, and it remains MISS on both sides, exactly as expected.

### Q11 — `replay edition workflow lineage mapping activation`

- Baseline/candidate relevant owner: `backend/src/cti_app/domain/discovery_cumulative.py` (`ReplayIdentityMapping`) — rank 5 both sides.
- Notes: **Regression vs R67** (PASS rank 2 → FAIL rank 5). R67's owners `replay_activator.py`/`replay_service.py` (`ReplayActivator`, the literal "activation" implementer per R67's own judgment) do not appear anywhere in top-8 on either snapshot here; instead the top 4 slots are taken by `test_replay_activator.py` and `test_replay_integration.py` test chunks (correctly excluded, but crowding the list), with the ADR pushed down to rank 6. `ReplayIdentityMapping` is a legitimate, independently-verified relevant hit (it implements the lineage-mapping data structure the query names), so it is counted, but the query still regresses from PASS to FAIL. `minimal_owner_files` here (1, based on what this replay actually verified) is narrower than R67's ground truth (3); this is flagged rather than silently reused, since the file this replay's ranking surfaced is genuinely different from R67's owner set.

### Q12 — `evidence pack coverage calculate contributions tracking`

- Baseline/candidate relevant owner: `coverage_calculator.py` (`new_contributions`) + `amendment_service.py` (`DeltaPackBuilder`) — rank 1 both sides, unchanged from R67 (same paths, near-identical line ranges). `minimal_owner_files` reused from R67 (2→2).

---

## Métriques agrégées

MISS queries: Q10 on both baseline and candidate. Excluded only from the rank statistic per the frozen rules — never from hit rates.

| Metric | Baseline | Candidate (R68) |
|---|---|---|
| top3_hit_rate | 8/12 = 66.7% | 8/12 = 66.7% |
| top8_hit_rate | 11/12 = 91.7% | 11/12 = 91.7% |
| median_first_relevant_rank (MISS excl., n=11) | 1 | 1 |
| mean_first_relevant_rank (MISS excl.) | 2.09 (23/11) | 2.36 (26/11) |
| median_minimal_owner_files | 1 | 1.5 |
| mean_minimal_owner_files | 1.333 (16/12) | 1.5 (18/12) |
| median_files_before_first_hit | 1.5 | 1.5 |
| mean_files_before_first_hit | 1.667 (20/12) | 1.917 (23/12) |
| median_lines_before_first_hit | 91 | 67.5 |
| mean_lines_before_first_hit | 85.25 (1023/12) | 91.9 (1103/12) |

Owner-files raw values (Q1–Q12 order):
- Baseline: 1, 1, 2, 1, 1, 1, 2, 2, 1, 1, 1, 2
- Candidate: 1, 1, 3, 2, 1, 1, 2, 2, 1, 1, 1, 2

Reduction calc:

    (baseline_median - final_median) / baseline_median * 100
    = (1 - 1.5) / 1 * 100
    = -50%

---

## Verdicts obligatoires

### A — Final top3 (>=80% required)

**FAIL** — candidate top3_hit_rate = 66.7%.

### B — Final owner files (median <=3 required)

**PASS** — candidate median_minimal_owner_files = 1.5.

### C — Reduction owner files (>=40% reduction required)

**FAIL** — reduction = -50% (median went from 1 to 1.5, not down).

---

## Métriques secondaires (ne remplacent aucun verdict)

- top8_hit_rate is unchanged at 91.7% both sides (all first-relevant hits found in this replay land within rank 8, Q10 excepted).
- mean_first_relevant_rank got slightly worse (2.09 → 2.36), driven by Q9 (4→7).
- median_files_before_first_hit unchanged (1.5); mean got worse (1.667 → 1.917), again driven by Q9's noisier candidate result set (6 distinct files skimmed before the hit, largely AGENTS/README/example/test chunks with inflated symbol-field scores).
- median_lines_before_first_hit improved (91 → 67.5); mean got slightly worse (85.25 → 91.9) — Q9's outlier (279 lines-before-hit) pulls the mean up while several other queries' chunks shrank.

None of these offset the FAIL verdicts on A and C.

---

## Interprétation (après calcul uniquement)

Compared directly against R67's original run (`R67_ab_results.md`), R68's field-aware lexical ranking did exactly what it was asked to do on the two problems named in the task brief: Q1's owner moved from rank 6/4 to rank 1/1 (its true owner, `ConversationLifecycle.release`, was never even inspected as a candidate before), and Q2 moved from a genuine MISS on both snapshots to rank 1/1 — both generic outcomes of weighting symbol-token matches (`FIELD_WEIGHT_SYMBOL = 3.0`) far above body-only occurrences, with zero query- or file-specific logic.

But the same generic change regressed three other queries that were previously passing or borderline: Q6 (rank 1→4, PASS→FAIL), Q9 (rank 4/1→4/7, PASS→FAIL on both), and Q11 (rank 2→5, PASS→FAIL). All three share a common mechanism: a markdown heading, a dataclass name, or a test-function name happened to contain the query's literal tokens as its "symbol" (per the frozen scoring rules a heading counts as a chunk's symbol under the markdown chunker, and this replay's `FIELD_WEIGHT_SYMBOL` does not distinguish "symbol of documentation" from "symbol of code"). Those chunks are still excluded from the relevant-hit count (AGENTS/README/tests never counted), but their inflated score raises the top score used by the `--relative-floor` cutoff (default 0.72, unchanged, per the frozen protocol), which appears to push some previously-passing, lower (but still legitimate) source hits below the floor entirely — most visibly for Q9, where `run_generation` (R67's owner, rank 1 in R67) is completely absent from top-8 in this replay on either snapshot.

Net effect on the aggregate top3 metric across the fixed 12-query set is therefore a *decrease*, not an increase: 9/12 (75.0%) in R67 → 8/12 (66.7%) here. Verdict A stays FAIL, now with a lower number than R67's own FAIL. Verdict C stays FAIL (owner-file reduction is negative either way — R67's -100%, this replay's -50% — driven by the refactor's monolith-splitting itself, not by ranking). Verdict B stays PASS in both.

This is reported plainly rather than adjusted: the task explicitly forbade query- or file-specific tuning to fix Q1/Q2/Q10, and a generic symbol/path-weighted ranking is exactly what was asked for. It fixed the two problems it targeted and did not regress any of R68's synthetic tests (all four pass, see gate below), but it is not an unambiguous net win on the frozen R67 benchmark — it trades wins on two queries for losses on three others via a side effect (doc-heading symbol inflation interacting with the relative-floor cutoff) that a purely field-aware weighting scheme does not, by itself, resolve. Distinguishing "symbol" by chunk *kind* (code symbol vs. doc heading) rather than by field alone would likely be the next generic step, but that is out of scope for this task's write allowlist and was not attempted.

### Limite méthodologique

Several `minimal_owner_files` calls in this replay were revised from R67's determination where R68's top-8 surfaced a different, independently-verified relevant file (Q1, Q6, Q9, Q11) rather than R67's originally-identified owner file — each is documented individually above, with the file's body inspected directly before being counted. Where the underlying application code is unchanged from R67's final snapshot and the same owner file was reconfirmed present, R67's `minimal_owner_files` count was reused as-is (Q3, Q4, Q5, Q7, Q8, Q10, Q12) rather than re-derived from scratch, since it is a property of the code, not of the ranking. None of these calls was revisited after seeing its effect on the aggregate numbers.

---

## Gate — tests unitaires (avant ce replay)

    uv run --python 3.12 --with pytest pytest scripts/ctx/tests/test_ctx.py -q
    7 passed in 11.67s

All four pre-existing R66 regression tests pass unchanged, plus three new R68 synthetic tests (none referencing any Q1/Q2/Q10 name or text):

- `test_exact_symbol_beats_narrative_test_mention` — an exact source symbol outranks a test file that narrates the same concept with more matching tokens.
- `test_source_implementation_beats_adr_narrative` — a source implementation outranks a doc/ADR that repeats the same words without implementing the behavior.
- `test_explicit_test_oriented_query_still_returns_test` — a query that explicitly asks for a test still retrieves it (field-aware ranking does not suppress legitimate test-oriented queries).
