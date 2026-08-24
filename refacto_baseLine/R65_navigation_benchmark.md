# R65 — Navigation Benchmark (current state)

Measurement only. No application code was modified while producing this
document.

## 1. Methodology

- Source of the 12 scenarios: the list named in `R00_PHASE_COMPLETE.md`
  ("R00.2 — Benchmark Queries").
- Tool: `uv run scripts/ctx/ctx.py query "<text>" -k 8`.
- Each scenario got one concrete, neutral query. Per `AGENTS.md`, at most
  one reformulation was allowed when a first result set looked
  insufficient. Reformulations are noted per row.
- For each scenario: `first_relevant_rank`, `relevant_in_top3`,
  `relevant_in_top8`, `minimal_distinct_files_needed`, `minimal_files`,
  `notes`. Excluded from "relevant"/"minimal files": `AGENTS.md`, test
  files not needed to *locate* the change, files only mentioned by
  transitivity, and results judged off-topic.

### 1.1 Infrastructure constraint encountered

`ctx.py build` and the dense-embedding path of `ctx.py query` both require
`BASE_URL` and `EMBEDDING_API_KEY` (an embedding gateway). Neither is set
in this session/sandbox, and `.env` was not inspected (excluded by
`AGENTS.md`'s "never inspect" list). This is an **unavailable
infrastructure/configuration** condition, not a code defect — the correct
response per project rules is to document it, not to work around it by
supplying credentials or editing `ctx.py`.

Consequence: `ctx.py build` could not refresh the index, and
`ctx.py query` could not use dense/semantic ranking. All 12 queries below
were run in degraded mode: `--lexical-only --no-refresh` against the
**existing, already-built index** (`built_at: 2026-08-22T00:16:12+0200`,
`status: stale` per `ctx.py status`). `--lexical-only` alone still failed
because `maybe_refresh()` runs before ranking and itself needs the
embedding client when the index is stale; `--no-refresh` was required to
bypass that refresh attempt and query the on-disk index as-is.

**This has a second, more serious consequence, discovered while
cross-checking results**: the index predates several refactor commits
landed on 2026-08-23 and 2026-08-24 (after the 2026-08-22 build), including
`R28` (`application/discovery.py` → package), the merge/cumulative package
split, and `R62`/`R63` (`App.tsx` reduced to a shell, `DiscoveryPanel.tsx`
and edition pages extracted). The stale index returned hits for file paths
that **no longer exist** at `HEAD`:
`backend/src/cti_app/application/discovery.py`,
`backend/src/cti_app/application/discovery_cumulative.py`, and
`backend/src/cti_app/infrastructure/database/models.py` (all now
directories/packages), plus one superseded Alembic migration filename.
Every path cited below was independently checked with
`git cat-file -e HEAD:<path>` before being counted as a real hit; a hit at
a path that no longer exists is scored as **not relevant** (a miss), and
is called out explicitly in `notes`.

Net effect: this benchmark measures `ctx.py` in a degraded, two-day-stale
configuration, not the tool's intended dense-search behavior against a
current index. The numbers below are a lower bound on current
navigability, not a clean verdict on `ctx.py` itself.

## 2. R00 baseline limitation

`R00_PHASE_COMPLETE.md` describes `R00_baseline_report.md`,
`R00_benchmark_queries.md`, and `R00b_agents_md_template.md` as produced
artifacts. None of the three exist in the working tree, and:

```
git log --all --oneline -- \
  refacto_baseLine/R00_baseline_report.md \
  refacto_baseLine/R00_benchmark_queries.md \
  refacto_baseLine/R00b_agents_md_template.md
```

returns **no commits at all** — these files were never committed to any
branch in this repository. They cannot be recovered.

**The detailed pre-refactor navigation benchmark was not preserved in the
repository; exact before/after per-query comparison is therefore not
verifiable.**

`R00_baseline_metrics.json` (the pytest baseline: 461/489 passing,
94.27%) *is* present and remains valid as a **test** baseline. It contains
no navigation/file-count data, so it cannot substitute for the missing
navigation benchmark.

## 3. Per-query results (current state, degraded lexical-only mode)

| # | Scenario | Query used | first_relevant_rank | top3 | top8 | minimal files | notes |
|---|----------|-----------|:---:|:---:|:---:|:---:|---|
| 1 | Conversation lifecycle / release | "conversation lifecycle release publication workflow" | 4 | No | Yes | 3 | Rank1 hit (`infrastructure/database/models.py`) is a **stale path** (now a `models/` package) — excluded. First valid relevant hit: `domain/model_conversations.py` `ConversationLifecycle.release` (r4). Files: `domain/model_conversations.py`, `chatgpt-bridge/server.py` (`release_conversation`, r7), `infrastructure/database/repositories/model_conversations.py` (r5). |
| 2 | Conversation persistence | "conversation repository save load conversation state to database" (reformulated once; first attempt returned only generic `<module>` chunks) | 1 | Yes | Yes | 2 | `infrastructure/database/repositories/model_conversations.py` (r1, r2). Files: that repository + `application/model_conversations.py` (service, not directly returned but the natural companion). |
| 3 | Discovery recovery | "discovery recovery incomplete operation retry after failure" | — (miss) | No | No | n/a | Only candidate hit, `application/discovery.py:354-390 DiscoveryService._resume_recovery_child` (r8), is a **stale path** — `application/discovery.py` no longer exists (became a package under `R28`). Reclassified as a miss. |
| 4 | Merge validation | "amendment merge validation conflict resolution logic" | 1 | Yes | Yes | 2 | Rank1 hit `domain/discovery_cumulative.py` (`ReplayIdentityMapping`) is a valid, existing path. Rank3 hit (`application/discovery_cumulative.py` `HumanMergePlanner`) is a **stale path** — real current file is `application/discovery/cumulative/service.py`, not surfaced. Files (confirmed valid): `domain/discovery_cumulative.py`, `infrastructure/database/repositories/discovery_cumulative.py` (r4). |
| 5 | Stale merge / replan | "stale amendment replan outdated merge handling" | — (miss) | No | No | n/a | Both candidate hits (`DiscoverySnapshotStaleError`, `resolve_merge_run`) point to `application/discovery_cumulative.py`, a **stale path** that no longer exists (moved to `application/discovery/cumulative/service.py` on 2026-08-23). Reclassified as a miss. |
| 6 | Collection | "subject collection service indexing and querying briefs and amendments for a subject" (reformulated once; first attempt matched `amendment_service.py`/`uow.py` generically) | — (miss) | No | No | n/a | Neither attempt surfaced `application/collection.py` (owner: `SubjectCollectionService`, confirmed present and >40KB in the section-7 inventory) or `application/collection_review.py`. A genuine navigation miss for this phrasing, independent of staleness. Direct structural check (§6 below) confirms the real current owners are `collection.py` + `collection_review.py` (2 files). |
| 7 | Create edition frontend | "create edition frontend form submit API call to persist a new edition" | — (miss) | No | No | n/a | Rank1 hit `frontend/src/App.tsx:258-367 EditionCreatePage` and rank2 hit `application/discovery.py discover_edition` are **both stale**: `App.tsx` is now a 40-line shell (moved to `frontend/src/pages/EditionCreatePage.tsx` on 2026-08-24) and `application/discovery.py` no longer exists as a flat file. Reclassified as a miss; the tool pointed confidently at code that has since moved. |
| 8 | Production workflow | "production workflow pipeline parse render publish stages orchestration" | 1 | Yes | Yes | 1 | `application/production_workflow.py` `<module>` (r1), valid path. Only 3 results returned in total (steep score drop-off after r1–r3). |
| 9 | ChatGPT bridge integration | "browser extension server routes request forwarding to ChatGPT conversation" (reformulated once; first attempt was dominated by unrelated tests) | 1 | Yes | Yes | 3 | `chatgpt-bridge/server.py` `<module>` (r1), `chatgpt-bridge/extension/content.js` `handlePrompt` (r6), `backend/src/cti_app/integrations/models.py` `ChatGPTBridgeTransport` (r8). All valid paths. |
| 10 | Editorial preservation | "published brief immutability amendment cannot alter approved content" (reformulated once; first attempt returned only tests/docs, no application code) | 4 | No | Yes | 2 | `application/amendment_service.py` `create_redactional_revision` (r4), `domain/briefs.py` `AmendmentStatus` (r5). Both valid paths. |
| 11 | Replay workflow | "replay workflow edition replay lineage preservation" | 6 | No | Yes | 2 | `application/replay_service.py` `replay_edition_discovery` (r6), `application/replay_activator.py` `ReplayActivator` (r7). Both valid; ranks 1–5 were tests/ADR docs. |
| 12 | Evidence coverage | "evidence pack coverage calculation tracking" | 1 | Yes | Yes | 1 | `application/coverage_calculator.py` `<module>` (r1), valid. A secondary hit at r7 (`migrations/versions/0021_editorial_preservation_increment_3.py`) is a stale path — expected, since migrations were later squashed to a single `0001_baseline.py` (consistent with the DoD, not a bug). |

4 of 12 scenarios (#3, #5, #6, #7) are misses. Three of those four (#3, #5,
#7) are directly attributable to the stale index pointing at paths that no
longer exist; #6 is a genuine phrasing/ranking miss independent of
staleness.

## 4. Aggregate metrics (computed only over the 8 scenarios with a real
   relevant hit; misses excluded, as no rank/file-count is defined for
   "not found")

- `first_relevant_rank`: values `[4, 1, 1, 1, 4, 1, 6, 1]` → **median 1**, mean 2.375
- `minimal_distinct_files_needed`: values `[3, 2, 2, 1, 3, 2, 2, 1]` → **median 2**, mean 2.0
- `top3_hit_rate` (over all 12 scenarios, misses count as "no"): **5/12 = 41.7 %**
- `top8_hit_rate` (over all 12 scenarios): **8/12 = 66.7 %**

## 5. Target verdicts

| Target | Result | Verdict |
|---|---|---|
| Median `minimal_distinct_files_needed` ≤ 3 | 2 (n=8/12, misses excluded) | **PASS**, but coverage is partial — 4/12 scenarios produced no usable file count at all. |
| Top1–3 hit rate ≥ 80 % | 41.7 % (5/12) | **FAIL** |
| ≥ 40 % reduction in files needed vs. pre-refactor | No numeric pre-refactor baseline survives (§2) | **NOT VERIFIABLE** |

No PASS is declared for the historical-reduction target, per instruction:
the pre-refactor navigation benchmark was never committed, so there is
nothing to diff against, and no percentage is invented.

The top1–3 FAIL is not an artifact of the staleness issue alone: even
restricted to the 8 scenarios where `ctx` returned a genuinely valid hit,
top3 was only reached in 5/8 (62.5 %), still under target.
