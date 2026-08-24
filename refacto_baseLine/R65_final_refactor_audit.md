# R65 — Final Refactor Audit

Measurement only. No application code, `AGENTS.md`, or R00 baseline file
was modified while producing this document.

Full navigation methodology, per-query data, and the infrastructure/index
caveats behind rows 9–10 live in
[`R65_navigation_benchmark.md`](R65_navigation_benchmark.md); this
document summarizes the structural checks (§6–8 of the R65 task) and the
12-point Definition-of-Done table.

## Structural owner checks (raw evidence)

**Bridge**
- `chatgpt-bridge/server.py` is a 12-line executable launcher; its
  docstring states explicitly "ce module est uniquement le launcher
  exécutable" and it imports `BridgeApplication` from `bridge.app`.
- `chatgpt-bridge/bridge/app.py` exists (13 KB) — composition root present.
- `grep -rn "from server import\|import server\b" chatgpt-bridge/bridge/`
  returned nothing — no `bridge → server` import.

**Frontend**
- `frontend/src/App.tsx`: 40 lines, 1408 bytes.
- `frontend/src/pages/`: `EditionCreatePage.tsx`, `EditionDetailPage.tsx`,
  `EditionListPage.tsx` present — Edition pages own that concern.
- `frontend/src/features/discovery/DiscoveryPanel.tsx` present — feature
  owner exists (also the largest frontend file at 40 KB, see inventory).

**Collection**
- `application/collection.py`: 41,288 bytes.
- `application/collection_review.py`: 6,570 bytes.
- `CollectionReviewService` (in `collection_review.py`) owns
  `list_evidence`, `decisions`, `get_claim`, `extracted_text`,
  `decide_claim`, `decide_indicator`, `decide_relationship`.
  `grep -ni "review" application/collection.py` returned nothing — no
  review method leaked back into `collection.py`.

**ORM**
- `find backend/src -iname "schema.py" -path "*models*"` → no match.
- `grep -rn "models.schema" backend/src backend/tests` → no match.

**Repositories**
- `find backend/src -iname "_shared.py" -path "*repositories*"` → no match.

**Migrations**
- `backend/migrations/versions/`: only `0001_baseline.py` (plus
  `__pycache__`), 1915 lines.
- `down_revision: str | None = None` confirmed in `0001_baseline.py`.

## Big-file inventory (git-tracked source, metadata only)

Excludes `backend/migrations/versions/0001_baseline.py` (documented
exception — exhaustive Alembic baseline).

| File | Size | Bracket |
|---|---:|---|
| `chatgpt-bridge/extension/content.js` | 55,786 B | > 50 KB |
| `backend/src/cti_app/api/discovery.py` | 45,211 B | 40–50 KB |
| `backend/src/cti_app/application/production_workflow.py` | 41,869 B | 40–50 KB |
| `backend/src/cti_app/application/collection.py` | 41,288 B | 40–50 KB |
| `frontend/src/features/discovery/DiscoveryPanel.tsx` | 40,089 B | 40–50 KB |
| `backend/src/cti_app/application/discovery/cumulative/service.py` | 37,591 B | 30–40 KB |
| `backend/src/cti_app/integrations/models.py` | 36,025 B | 30–40 KB |
| `backend/src/cti_app/application/production_parsers.py` | 35,052 B | 30–40 KB |
| `backend/src/cti_app/application/model_gateway.py` | 35,051 B | 30–40 KB |
| `backend/src/cti_app/application/briefs.py` | 31,655 B | 30–40 KB |

None of the 10 files above (nor their directory) has a companion
`AGENTS.md` documenting a size/coherence rationale (checked directly —
`models/`'s and `repositories/`'s existing `AGENTS.md`s cover different
files). No content-level read was done to judge internal cohesion, per the
task's "no bulk reads" constraint, so none is called "coherent" on
evidence, and none is called "action-needed" either — no concrete defect
was observed, only undocumented size. Verdict for all 10:
**future-audit** — flagged, not fixed here.

## Final gates (as actually run)

**Backend** (`cd backend`)
- `uv run pytest -q`: **8 failed, 477 passed, 66 skipped** in 38.60s.
  Skips are all `TEST_POSTGRES_ADMIN_DSN` integration tests (infra, as at
  R00). Failures are all `ValueError` test-data-validation errors (SHA-256
  hash format, contribution/paragraph constraints) — the same category R00
  recorded (10 failures); 2 of R00's original 10 no longer fail, 8 remain,
  no new failure categories appeared. Not fixed here, per instruction.
- `uv run ruff check .`: **All checks passed.**
- `uv run mypy src tests`: **Success: no issues found in 218 source files.**

**Frontend** (`cd frontend`)
- `corepack pnpm test --run`: **47 passed, 12 files, 0 failed.**
- `corepack pnpm typecheck`: clean, no output.
- `corepack pnpm lint`: **0 errors, 5 warnings** (pre-existing —
  `react-hooks/exhaustive-deps` in `DiscoveryPanel.tsx`,
  `react-refresh/only-export-components` ×4 in `editionPresentation.tsx`
  and `routing.tsx`); `prettier --check .` clean. Warnings left as-is, not
  converted into refactor work.

**ChatGPT bridge — Python** (`cd chatgpt-bridge`)
- `uv run --python 3.12 --with-requirements requirements-test.txt python -m pytest tests/ -q --tb=short`:
  **65 passed.**

**ChatGPT bridge — JS**
- `node --test tests/completion.test.js tests/content-dom.test.js tests/final-output.test.js`:
  **3 passed, 0 failed.**

No STOP-triggering red test was found in the gates themselves (the 8
backend failures are pre-existing test-data-validation issues, already
known at R00, not new regressions). The STOP conditions that *did* trigger
are the missing R00 baseline docs (§2 of the benchmark doc) and the
stale/unrebuildable `ctx.py` index (§1.1) — both documented, neither
"fixed" here.

## Definition-of-Done — 12 points

| # | Item | Verdict | Evidence | Remaining action |
|---|---|:---:|---|---|
| 1 | Empty PostgreSQL baseline | **PASS** | Single `0001_baseline.py`, `down_revision=None`; no legacy migration chain. | — |
| 2 | Legacy removed | **PASS** | `models/schema.py` absent, `repositories/_shared.py` absent, no dangling `models.schema` imports. | — |
| 3 | ORM/DB consistency | **PASS** | Same evidence as #2. | — |
| 4 | Mega files / launchers | **PASS** (launcher/composition-root specifically) | `server.py` is a 12-line launcher delegating to `bridge.app.BridgeApplication`; no `bridge → server` import. Broader "mega file" question tracked separately at #12. | — |
| 5 | `App.tsx` lightweight shell | **PASS** | 40 lines / 1408 bytes; Edition pages and `DiscoveryPanel` own their features. | — |
| 6 | Exhaustive Alembic metadata | **PASS** | One baseline revision, 1915 lines, `down_revision=None`. | Full schema-completeness was checked structurally, not re-read line-by-line, in this pass. |
| 7 | Direct semantic imports | **NOT VERIFIABLE** | R65's structural-check list (§6 of the task) does not define a concrete probe for this item; none was run this session. | Define and run a concrete check if this DoD item needs to be closed. |
| 8 | Bridge destructive invariants | **NOT VERIFIABLE** | `AGENTS.md` states the rule (verified external identity; never title/position/DOM-index/visual-similarity), but no targeted check for it was specified or run in R65. | Define and run a concrete check (e.g. grep for identity-verification call sites in the extension/bridge). |
| 9 | Navigation median files ≤ 3 | **PASS**, partial coverage | Median 2 files over the 8/12 scenarios that produced a real hit; see benchmark doc §3–5. | 4/12 scenarios (Q3, Q5, Q6, Q7) produced no file count at all (misses) and are excluded from this median — re-run once the index is rebuilt (see #10). |
| 10 | Top1–3 hit rate ≥ 80 % | **FAIL** | 5/12 = 41.7 % overall; even restricted to the 8 scenarios with a valid hit, 5/8 = 62.5 %, still under target. | Rebuild the `ctx.py` index with valid `BASE_URL`/`EMBEDDING_API_KEY` credentials and re-run the 12-query benchmark. 3 of the 4 current misses (Q3, Q5, Q7) are attributable to the index being 2 days stale relative to `R28`/`R62`/`R63` file moves, not necessarily to the tool's underlying ranking; Q6 is a genuine phrasing/ranking miss and should be re-investigated regardless. |
| 11 | ≥ 40 % fewer files vs. pre-refactor | **NOT VERIFIABLE** | `R00_baseline_report.md` / `R00_benchmark_queries.md` / `R00b_agents_md_template.md` were never committed to any branch (`git log --all` on those three paths returns nothing) — no numeric pre-refactor baseline survives to diff against. | None available — this cannot be recovered; any future refactor phase should commit its baseline artifacts immediately, before they can be lost. |
| 12 | Remaining big files coherent/scheduled | **NOT VERIFIABLE** | 10 files over 30 KB (1 > 50 KB, 4 in 40–50 KB, 5 in 30–40 KB, see inventory above); none carries a documented size/coherence rationale. | Schedule a dedicated audit pass over the 10 listed files; do not treat this list as a refactor backlog opened by R65. |

## Overall

**Refactor is not declared complete.**

Six of twelve DoD items are clean PASSes on direct structural evidence
(1–6). Two items have no defined check in this session and are honestly
NOT VERIFIABLE rather than assumed (7, 8). Item 9 (median files) passes on
the data that exists, but that data covers only 8/12 scenarios. Item 10
(top1–3 hit rate, the refactor's headline navigation goal) is a genuine
**FAIL** at 41.7 % against an 80 % target — this is not a documented
warning or pre-existing debt item, it is a numeric miss on the primary
target this refactor was measured against, so "refactor complete" cannot
be claimed. Item 11 is legitimately not verifiable (baseline never
committed, not omitted by this audit). Item 12 is left as an explicit,
un-actioned future-audit list rather than either asserted "coherent" or
turned into new refactor work here.

The cleanest next step is **not** more refactoring: it is restoring the
measurement infrastructure (embedding credentials for `ctx.py build`) and
re-running this same 12-query benchmark against a current index, since 3
of the 4 present misses look like artifacts of a 2-day-stale index rather
than of the current code layout.
