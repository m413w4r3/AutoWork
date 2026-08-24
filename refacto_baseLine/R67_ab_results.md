Benchmark spec commit: 3becd2a9be9a1388b32a5096d347586a4726360a
Baseline: 2c7a15d1bfe905554f64d560689c5f9e111fc400
Final: 882c8a695c8df8144aba194f8ad190ba36456865
ctx.py: 882c8a695c8df8144aba194f8ad190ba36456865

Engine mode for all 24 queries: `--lexical-only`, `-k 8`, default per-file / relative floor / lexical-meta weights, no embeddings, no `.env`, no `--path`. Fresh index built per snapshot (`build --lexical-only`). Both worktrees' `status` reported `source_index: current`, `dense_embeddings: incomplete` (expected — lexical-only).

---

## Résultats — tableau

| Q | base rank | final rank | base top3 | final top3 | base owner | final owner | base files-before-hit | final files-before-hit | base lines-before-hit | final lines-before-hit |
|---|---|---|---|---|---|---|---|---|---|---|
| Q1 | 6 | 4 | FAIL | FAIL | 1 | 1 | 5 | 3 | 417 | 225 |
| Q2 | MISS | MISS | FAIL | FAIL | 1 | 1 | 3 | 3 | 250 | 250 |
| Q3 | 1 | 1 | PASS | PASS | 2 | 3 | 1 | 1 | 23 | 23 |
| Q4 | 1 | 1 | PASS | PASS | 1 | 2 | 1 | 1 | 106 | 106 |
| Q5 | 1 | 1 | PASS | PASS | 1 | 1 | 1 | 1 | 47 | 106 |
| Q6 | 1 | 1 | PASS | PASS | 1 | 1 | 1 | 1 | 48 | 48 |
| Q7 | 1 | 1 | PASS | PASS | 2 | 2 | 1 | 1 | 110 | 110 |
| Q8 | 2 | 2 | PASS | PASS | 2 | 2 | 2 | 2 | 192 | 192 |
| Q9 | 1 | 1 | PASS | PASS | 1 | 2 | 1 | 1 | 92 | 92 |
| Q10 | MISS | MISS | FAIL | FAIL | 1 | 1 | 2 | 2 | 87 | 87 |
| Q11 | 2 | 2 | PASS | PASS | 3 | 3 | 2 | 2 | 118 | 118 |
| Q12 | 1 | 1 | PASS | PASS | 2 | 2 | 1 | 1 | 59 | 59 |

---

## Détail par requête

### Q1 — `conversation lifecycle release after discovery amendment production publication`

- Baseline relevant owner(s): `chatgpt-bridge/server.py` (`release_conversation`, `mark_conversation_deleted`)
- Final relevant owner(s): `chatgpt-bridge/bridge/routes_conversations.py` (`ConversationRoutes.release_conversation`, `ConversationRoutes.mark_conversation_deleted`)
- Notes: The query's core action is "release" (the word appears alone, the other terms name the pipeline stages that precede it). `release_conversation` is a literal, exact match and is scored as the first relevant hit. `DiscoveryService.discover_edition` (rank 1 baseline / rank 1 final) orchestrates the *discovery* stage, not the release action itself, and was NOT counted as relevant — it doesn't implement or orchestrate "release." Both `release_conversation` and `mark_conversation_deleted` stayed co-located in a single file across the refactor (moved from the `server.py` monolith into a dedicated `routes_conversations.py`), so owner-file count is unchanged (1→1) even though rank improved (6→4).

### Q2 — `persist and reload model conversation state database repository`

- Baseline relevant owner(s): `backend/src/cti_app/infrastructure/database/repositories.py` (`SqlAlchemyModelConversationRepository`, `SqlAlchemyModelConversationTurnRepository`) — NOT in top 8, MISS.
- Final relevant owner(s): `backend/src/cti_app/infrastructure/database/repositories/model_conversations.py` (same two classes) — NOT in top 8, MISS.
- Notes: Ground truth located via targeted rg (`class SqlAlchemyModelConversation`) in each snapshot since both queries returned only 3 low-relevance candidates (browser-extension UI handler, an unrelated edition-repository delete method, and discovery-edition orchestration) — none is a repository implementing persistence of *model conversation* state. Genuine MISS both sides; the refactor split the 900+-line `repositories.py` into per-aggregate files (the conversation repository shrank from being buried in a 900-line file to a dedicated 315-line file) but this did not surface it in lexical top-8 either before or after.

### Q3 — `recover incomplete discovery operation after failed or interrupted model run`

- Baseline relevant owner(s): `chatgpt-bridge/server.py` (`RunRegistry.recover_interrupted`) + `backend/src/cti_app/application/discovery.py` (`_research_or_resume`, `_resume_recovery_child`)
- Final relevant owner(s): `chatgpt-bridge/bridge/registry.py` (`RunRegistry.recover_interrupted`) + `backend/src/cti_app/application/discovery/service.py` (`_research_or_resume`, branching only) + `backend/src/cti_app/application/discovery/recovery.py` (`DiscoveryRecoveryCoordinator`, the actual resume/recovery mechanics `_research_or_resume` delegates to)
- Notes: `RunRegistry.recover_interrupted` (bridge-restart recovery of in-flight runs) verified by reading its body — it is a direct, literal implementation of "recover ... after ... interrupted model run." First relevant rank is 1 on both sides. Owner-file count went **up** (2→3): baseline's `discovery.py` held both the resume-branching logic and the actual recovery mechanics in one file; the final state (R27 extraction, per the module docstring in `recovery.py`) split that into a thin-branching `service.py` and a dedicated `DiscoveryRecoveryCoordinator` in `recovery.py`, adding a file to the owner set for this scenario.

### Q4 — `validate discovery merge conflicts before applying cumulative merge`

- Baseline relevant owner(s): `backend/src/cti_app/application/discovery_cumulative.py` (`CumulativeDiscoveryService._resolve_merge_run`, `validate_merge_plan`)
- Final relevant owner(s): `backend/src/cti_app/application/discovery/cumulative/service.py` (`_resolve_merge_run`) + `backend/src/cti_app/application/discovery/cumulative/validation.py` (`validate_merge_plan`)
- Notes: `validate_merge_plan` body inspected directly — it raises `ValueError` on unknown handles/incoming coverage/conflicting evidence, an exact match for "validate ... merge conflicts." `_resolve_merge_run` orchestrates applying merge decisions and is scored relevant too (rank 1 both sides). Owner-file count went **up** (1→2): the refactor split validation into its own `validation.py` module, away from the resolve-run orchestration in `service.py`.

### Q5 — `detect stale discovery merge and replan outdated merge run`

- Baseline relevant owner(s): `backend/src/cti_app/application/discovery_cumulative.py` (`CumulativeDiscoveryService.resolve_merge_run` / `_resolve_merge_run`, raising `DiscoverySnapshotStaleError` with `replan=...`)
- Final relevant owner(s): `backend/src/cti_app/application/discovery/cumulative/service.py` (same methods, same stale-detect/replan logic)
- Notes: Verified via targeted rg that the actual "stale by construction... raise DiscoverySnapshotStaleError(..., replan=...)" logic lives inside `_resolve_merge_run` in both snapshots. `errors.py` (final) merely holds the exception class definition and was not counted as an owner file — a bounded fix to the staleness *condition* only requires editing `service.py`. Rank 1 both sides, owner-file count unchanged (1→1), but line count needed roughly doubled (47→106) because baseline's shorter `resolve_merge_run` header chunk (47 lines) was superseded in final by a single larger `_resolve_merge_run` chunk (106 lines) as first hit — ctx's chunk boundaries differ, not the underlying logic size.

### Q6 — `brief amendment repository indexing querying storage retrieval`

- Baseline relevant owner(s): `backend/src/cti_app/api/production.py` (`save_brief_draft`)
- Final relevant owner(s): `backend/src/cti_app/api/production.py` (`save_brief_draft`, unchanged path/lines)
- Notes: No dedicated `AmendmentRepository` class exists in either snapshot (verified by rg — zero matches for `class.*Amendment.*Repository`). `BriefAmendment` (domain/briefs.py, rank 2) is a plain dataclass, not a repository, and was **not** counted as relevant. `save_brief_draft` (rank 1 both sides) is the nearest genuine match — it orchestrates append/get-current storage-and-retrieval of brief artifacts via `uow.production_artifacts`, close enough to "repository ... storage retrieval" for a brief-adjacent (draft, not literally "amendment") artifact to count as relevant. This is a judgment call, documented per spec.

### Q7 — `create edition frontend form submit API persistence`

- Baseline relevant owner(s): `frontend/src/App.tsx` (`EditionCreatePage`) + `frontend/src/api/editions.ts` (`createEdition`)
- Final relevant owner(s): `frontend/src/pages/EditionCreatePage.tsx` (`EditionCreatePage`) + `frontend/src/api/editions.ts` (`createEdition`, unchanged)
- Notes: Both snapshots already had a separate frontend API-client module; the refactor only extracted the `EditionCreatePage` component out of the `App.tsx` monolith into its own page file. Owner-file count unchanged (2→2); rank unchanged (1→1).

### Q8 — `production workflow orchestrate parse render publish stages`

- Baseline relevant owner(s): `backend/src/cti_app/application/production_workflow.py` + `backend/src/cti_app/application/production_stages.py` (`BriefAssemblyService`)
- Final relevant owner(s): same two files, same line ranges
- Notes: Rank-1 result in both is a frontend test file (excluded per scoring rules), so first relevant rank is 2 on both sides. This module pair was not touched by the refactor between these two SHAs — identical paths and near-identical line ranges.

### Q9 — `ChatGPT bridge browser extension server request conversation routing`

- Baseline relevant owner(s): `chatgpt-bridge/server.py` (`run_generation`)
- Final relevant owner(s): `chatgpt-bridge/bridge/generation.py` (`run_generation`) + `chatgpt-bridge/bridge/routes_openai.py` (the actual HTTP routing that calls `run_generation`, verified by rg)
- Notes: Rank 1 both sides. Owner-file count went **up** (1→2): baseline's monolithic `server.py` held both the generation logic and the request-routing in one file; final splits generation (`generation.py`) from routing (`routes_openai.py`), so understanding "request ... routing" now requires reading both.

### Q10 — `published brief immutability amendment preservation rules`

- Baseline relevant owner(s): `backend/src/cti_app/application/amendment_service.py` — NOT in top 8, MISS.
- Final relevant owner(s): `backend/src/cti_app/application/amendment_service.py` (unchanged path) — NOT in top 8, MISS.
- Notes: All 3 returned candidates on both sides are a test file and an ADR doc — both excluded categories per the frozen scoring rules, so MISS was not even close in either snapshot. Ground truth located via targeted rg (`immutab`) — `amendment_service.py` explicitly documents "Published content is immutable; amendments reference it explicitly" and enforces it (`if amendment.status != AmendmentStatus.PUBLISHED`). This file did not surface in lexical top-8 on either snapshot.

### Q11 — `replay edition workflow lineage mapping activation`

- Baseline relevant owner(s): `backend/src/cti_app/application/replay_activator.py` (`ReplayActivator`) + `backend/src/cti_app/application/replay_service.py` (`ReplayService`) + `backend/src/cti_app/domain/discovery_cumulative.py` (`ReplayIdentityMapping`)
- Final relevant owner(s): same three files, same paths (this cluster was not touched by the refactor)
- Notes: Rank-1 result on both sides is an ADR doc (excluded), so first relevant rank is 2 both sides. `ReplayActivator` is the direct, literal implementer of "activation." Paths and owner-file count are identical baseline vs. final (3→3).

### Q12 — `evidence pack coverage calculate contributions tracking`

- Baseline relevant owner(s): `backend/src/cti_app/application/coverage_calculator.py` (`new_contributions`) + `backend/src/cti_app/application/amendment_service.py` (`DeltaPackBuilder`)
- Final relevant owner(s): same two files, same paths (unchanged, off-by-one line shifts only)
- Notes: Rank 1 both sides; both files address distinct parts of the query ("coverage calculate contributions" vs. "evidence pack ... tracking"), so both counted as owners. Unchanged between snapshots.

---

## Métriques agrégées

MISS queries: Q2, Q10 on both baseline and final. Excluded only from the rank statistic per the frozen rules — never from hit rates.

| Metric | Baseline | Final |
|---|---|---|
| top3_hit_rate | 9/12 = 75.0% | 9/12 = 75.0% |
| top8_hit_rate | 10/12 = 83.3% | 10/12 = 83.3% |
| median_first_relevant_rank (MISS excl.) | 1 | 1 |
| mean_first_relevant_rank (MISS excl.) | 1.7 (17/10) | 1.5 (15/10) |
| median_minimal_owner_files | 1 | 2 |
| mean_minimal_owner_files | 1.5 (18/12) | 1.75 (21/12) |
| median_files_before_first_hit | 1 | 1 |
| mean_files_before_first_hit | 1.75 (21/12) | 1.583 (19/12) |
| median_lines_before_first_hit | 99 (avg of 92, 106) | 106 (avg of 106, 106) |
| mean_lines_before_first_hit | 129.1 (1549/12) | 118.0 (1416/12) |

Owner-files raw values (Q1–Q12 order):
- Baseline: 1, 1, 2, 1, 1, 1, 2, 2, 1, 1, 3, 2
- Final: 1, 1, 3, 2, 1, 1, 2, 2, 2, 1, 3, 2

Reduction calc:

    (baseline_median - final_median) / baseline_median * 100
    = (1 - 2) / 1 * 100
    = -100%

---

## Verdicts obligatoires

### A — Final top3 (>=80% required)

**FAIL** — final top3_hit_rate = 75.0%.

### B — Final owner files (median <=3 required)

**PASS** — final median_minimal_owner_files = 2.

### C — Reduction owner files (>=40% reduction required)

**FAIL** — reduction = -100% (the median minimal-owner-file count went **up**, from 1 to 2, not down).

---

## Métriques secondaires (ne remplacent aucun verdict)

- top8_hit_rate is unchanged at 83.3% both sides.
- mean_first_relevant_rank improved slightly (1.7 → 1.5).
- mean_files_before_first_hit improved slightly (1.75 → 1.583).
- median_lines_before_first_hit got slightly worse (99 → 106); mean_lines_before_first_hit improved (129.1 → 118.0).
- median_files_before_first_hit is unchanged at 1.

None of these offset the FAIL verdicts on A and C.

---

## Interprétation (après calcul uniquement)

Verdict A (top3 hit rate) is flat, not improved: the two MISS scenarios (Q2, Q10) are MISS on both snapshots for the same underlying reason — the actual owner file (a per-aggregate repository for Q2, the amendment service for Q10) never surfaces in the lexical top-8 on either snapshot, so the refactor did not change the outcome for those two queries. The one query where rank changed materially (Q1: 6→4) still landed outside top3 on both sides.

Verdict C (owner-file reduction) is negative because several scenarios that spanned one large, multi-concern file in the baseline (`discovery_cumulative.py`, `discovery.py`, `server.py`) now span two or three smaller, single-concern files after the split (Q3: 2→3, Q4: 1→2, Q9: 1→2). This is a direct, mechanical consequence of the refactor's own stated goal (splitting monoliths into per-concern modules): a bounded change that used to touch one large file, in the cases that crossed responsibility boundaries (validation vs. orchestration, routing vs. generation, branching vs. recovery mechanics), now requires opening more (smaller) files to find the same amount of logic. Scenarios that lived entirely within a single concern already (Q1, Q5, Q6, Q7, Q8, Q10, Q11, Q12) kept the same owner-file count in both directions.

### Limite méthodologique

Documented after the verdicts, as required: two of the twelve queries (Q6, `save_brief_draft` as a stand-in for "amendment"; and the "activation/orchestration" calls in Q1/Q3/Q4/Q5) required a judgment call about whether an adjacent-but-not-identical concept counts as "directly implementing" the requested behavior. These calls are documented individually above; none of them was revisited after seeing its effect on the aggregate numbers, and none of the frozen thresholds, queries, or scoring rules were altered in response to results, per the R67a spec commit.
