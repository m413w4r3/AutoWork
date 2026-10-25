# Mission 05 / Phase 3 — test and CI ledger

Every entry records what was **actually executed**. `NOT_RUN`, `SKIPPED` and
`BLOCKED` are reported as such, never converted to PASS. Durations are only
recorded when measured by the command output. No external CTI/provider call was
made; all transports are local fakes.

## Environment

- Date: 2026-10-25. Host: Linux 6.8.0-146-generic.
- Repo: `/home/nill/perso/work/AutoWork`, local `main`.
- Baseline code SHA: `55fba67d5882c1ae6c4abff4e74c53a24e57649f`
  (`origin/main` identical at preflight). Mission bookkeeping commit:
  `0db6a9048d5ca356de270be833698def8a35eb92` (docs only).
- Python: `backend/.venv/bin/python` 3.12.3, invoked with
  `UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv uv run --no-sync --python 3.12`.
- PostgreSQL: dedicated `postgres:16-alpine` compose service `postgres-test`
  on `127.0.0.1:55432`; each test creates/drops its own database
  (`TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres`).
  No production database touched.
- Typst: pinned 0.15.1 installed locally by `scripts/install-typst.sh` to
  `$HOME/.local/share/autowork-tools/typst` (the `/snap/bin/typst` wrapper is
  unusable on this host). `TYPST_BINARY` points at the pinned binary.
- Poppler: `pdftotext` 24.02.0. D2: `$HOME/.local/bin/d2`. Fonts:
  repository `chpTypst` bundle.
- Object storage: filesystem `BlobStore` under test `tmp_path` unless an entry
  says otherwise. No live MinIO.
- Network: no provider/Bridge/VT/Shodan/CTI endpoint contacted. Tests assert
  fake-send counts and absence of egress locally.

## Baseline reference runs (before any code lot)

| Command (cwd `backend`) | Result | Duration |
|---|---|---|
| `pytest -m integration tests/integration/test_investigation_vertical_postgres.py::test_real_postgres_http_bridge_vertical_investigation_and_v6_publication` | PASS, 1 passed | 7.97 s |
| `pytest -m integration tests/integration/test_analyst_opinion_publication_postgres.py` | PASS, 1 passed (real Typst/Poppler path) | 3.55 s |
| `pytest -m integration tests/integration/test_investigation_acquisition_postgres.py` | PASS, 6 passed | 5.58 s |
| `pytest tests/test_advisor_consultation.py tests/test_advisor_wire.py tests/test_advisor_completeness.py tests/test_advisor_anti_slop_eval.py tests/test_operator_tool_invocation.py` | PASS, 108 passed | 2.76 s |
| `pytest tests/test_investigation_advisor_tools.py` | PASS, 125 passed | 3.75 s |
| `make test-backend` (full non-integration suite) at baseline 55fba67d | PASS, 4904 passed, 2 skipped, 31 deselected | 137.51 s |
| `make test-backend` after A+F integration (`57960058`) | PASS, 4912 passed, 2 skipped, 31 deselected | 137.29 s |
| golden replay at baseline `55fba67d` (copied `test_advisor_prompt_golden.py` into a detached worktree) | expected RED: 4 passed, 2 failed — failures are exactly the private-renderer inversion tests | 0.91 s |

Baseline anti-slop prompt fingerprint (observed in the test output):
advisor wire `advisor-wire.v1`, response `advisor-response.v3`, source parser
`advisor-source-parser.v3`, advisor prompt SHA-256
`54c9f454f0e80c95e518279c6a3205a776714db84964039a1fdaa0c7ebecafab`,
investigation prompt SHA-256
`4b775cab6d5d03fd30eee6c18981efc7ad20ffffd732b248e066e4649fbb2f71`.
Any M05-A byte change would be visible here.

## Per-lot runs

| Lot | SHA | Command | Result | Notes |
|---|---|---|---|---|
| M05-DOCS | 0db6a90 | docs only | NOT_APPLICABLE | registry + briefs |
| M05-A | 2e7733be | advisor suite (golden + consultation + wire + completeness + anti-slop + operator invocation) | PASS, 114 passed, 1 skipped (archived Run12 corpus absent) | 2.12 s, independent parent rerun |
| M05-A | 2e7733be | `tests/test_investigation_advisor_tools.py` | PASS, 125 passed | 3.18 s |
| M05-A | 2e7733be | ruff check on changed source/tests; mypy on advisor modules | PASS | — |
| M05-A | 2e7733be | anti-slop advisor prompt SHA | unchanged `54c9f454f0e80c95e518279c6a3205a776714db84964039a1fdaa0c7ebecafab` | byte parity |
| M05-A | integrated `b8a10165` | golden + 6 advisor files + `test_investigation_advisor_tools.py` on main | PASS, 240 passed | 5.37 s |
| M05-F | base `7c549ec3` | RED regression test replay | FAIL as expected: `UnboundLocalError` at `investigation_advisor_tools.py:1006` | 1.14 s |
| M05-F | candidate `a1b356c6` | same regression test | PASS, 1 passed | 0.53 s |
| M05-F | integrated `12671c6a` | advisor tools + golden + 6 advisor files on main | PASS, 241 passed | 5.39 s |
| M05-A + integration fix `7e51912a` | — | `make lint-backend` | PASS (849 files formatted); one import-order error found after integration and fixed mechanically without behaviour change | — |
| M05-A | integrated `7e51912a` | `make typecheck-backend` | PASS, no issues in 822 source files | — |
| M05-A | integrated `7e51912a` | `make test-ci-scripts` | PASS, 5 tests | 0.001 s |
| M05-B | 80076e6a → integrated `8da3300c` | `pytest -m integration tests/integration/test_investigation_e2e_no_sample_postgres.py` | PASS, 1 passed | 4.55 s integrated main; 4.00 s implementer; 4.03 s reviewer |
| M05-C | 38d9312d → integrated `222942e2` | `pytest -m integration tests/integration/test_investigation_e2e_synthetic_sample_postgres.py` | PASS, 1 passed | 5.17 s integrated main; 4.68 s implementer; 4.86 s reviewer |
| M05-D | 59f8c85c (candidate) | `pytest -m integration tests/integration/test_e2e_phase3_adversarial_postgres.py` | PASS, 2 passed | 4.56 s implementer; 2.98 s on f9c779a3 parent |
| M05-D | 59f8c85c | full gate set reported by D (lint, typecheck, test-backend, test-integration, workflow fast/full, CI scripts) | lint/typecheck PASS; test-backend 2 `filesystem_confinement_unavailable` failures in the agent sandbox (parent passes `test_local_analysis_commands.py` 23/23); integration 347 passed/142.36 s; workflow 22/32 passed; CI scripts 5 passed | reviewer independently confirmed integration 347 passed and no confinement failures in parent-class environment |
| M05-D | 85af3430 (correction after REVISE) | focused adversarial module | PASS, 3 passed | 5.06 s parent; 5.32 s reviewer |
| M05-D | 85af3430 | B and C drivers replayed on the D tree | PASS, 2 passed | 7.06 s parent |
| M05-D | 1667418c (final correction) | focused adversarial module | PASS, 3 passed | 5.35 s parent; 5.14 s implementer |
| M05-IDFIX | 70ffb9df → `25de7308` | `make test-integration` before/after reserving literal edition codes | **RED** 1 failed / 347 passed (`CT` collided, `add_if_absent` False) → **GREEN** 348 passed | 120.16 s → 115.78 s; root cause was the shared session DB plus allocator reaching a literal code |

## Global gates (after integration on local main)

| Gate | Result |
|---|---|
| `make lint-backend` at final code `25de7308` | **PASS** — Ruff checks passed, 852 files already formatted |
| `make typecheck-backend` at final code `25de7308` | **PASS** — no issues in 825 source files |
| `make test-backend` at `70ffb9df` | **PASS**, 4912 passed, 2 skipped, 31 deselected in 131.68 s. The only later commit (`25de7308`) is an integration-test file ignored by this target. |
| `make test-integration` at `25de7308` (full) | **PASS**, 348 passed in 115.78 s with the pinned Typst binary, font bundle and loopback PostgreSQL. Includes both E2E drivers and the 3 adversarial tests. |
| `test_agentic_correction_loop_postgres.py` isolated | PASS, 1 passed in 3.86 s with pinned Typst on PATH; without it, it resolves the broken snap wrapper. Environmental only. |
| `make test-workflow` at `25de7308` | **PASS**, 22 passed, 15 deselected in 60.61 s, JUnit guard OK |
| `make test-workflow-full` at `25de7308` | **PASS**, 32 passed, 5 deselected in 103.46 s, JUnit guard OK |
| `make test-ci-scripts` at `70ffb9df` | **PASS**, 5 tests. No CI script or tested file changed afterwards. |
| frontend gates | NOT_RUN — backend/test-only change; no frontend path touched |
| live providers / MinIO / S3 | NOT_DEMONSTRATED — explicitly out of scope and still blocked |
| full E2E-0 + E2E-1 in a recreated environment | NOT_RUN |

## Corrective closure pass (C1–C4)

Code SHA under the final closure gates: `4111acf3` (published baseline
`a30a7c22` + four test-only commits). Environment: Python 3.12.3, disposable
`postgres-test` on `127.0.0.1:55432`, pinned Typst 0.15.1, Poppler 24.02.0,
font bundle `chpTypst`, `TMPDIR=$HOME/.cache/awtmp`. No live provider/Bridge/
VT/Shodan/S3/MinIO call.

| Check | Result | Notes |
|---|---|---|
| Four targeted oracles (E2E-0, E2E-1, P10, in-flight) at `ca78f1e4` | **PASS**, 4 passed | 8.43 s |
| Revised P10 + both recovery scenarios at `4111acf3` | **PASS**, 3 passed | 3.37 s |
| `make lint-backend` at `4111acf3` | **PASS** | 854 files already formatted |
| `make typecheck-backend` at `4111acf3` | **PASS** | no issues in 827 source files |
| `make test-backend` at `ca78f1e4` | **PASS**, 4912 passed, 2 skipped, 31 deselected | 133.04 s; the later commits are integration-test-only and ignored by this target |
| `make test-integration` at `ca78f1e4` | **RED → GREEN** | First full run: 1 failed / 349 passed (`test_agentic_correction_loop_postgres` resolved the snap `typst` wrapper). Isolated rerun with the pinned binary on `PATH`: PASS 3.74 s. Full rerun: 350 passed in 145.68 s. |
| `make test-integration` at `7b896f20` | **PASS**, 351 passed | 149.59 s; adds the proven-no-send recovery scenario |
| `make test-integration` at `4111acf3` | **PASS**, 351 passed | 151.44 s; includes both E2E drivers, the P10 test and both recovery scenarios |
| `make test-workflow` at `ca78f1e4` | **PASS**, 22 passed, 15 deselected | 62.31 s, JUnit guard OK; later commits are integration-test-only and not collected by this target |
| `make test-workflow-full` at `ca78f1e4` | **PASS**, 32 passed, 5 deselected | 115.37 s, JUnit guard OK |
| `make test-ci-scripts` at `ca78f1e4` | **PASS**, 5 tests | no CI script changed afterwards |
| Frontend gates | **NOT_RUN** | no frontend path touched |
| GitHub CI run #40210449466 on published `a30a7c22` | **SUCCESS** | 5/5 jobs: backend, typst-renderer-smoke, W10+J2 browser, frontend, compose. The backend job ran ruff/format/mypy, the non-workflow pytest with the PostgreSQL service (integration included) and workflow FAST/FULL. |
| GitHub CI on the new local commits | **NOT_RUN** | The commits are unpushed; no push was performed in this closure. |

The RED → GREEN integration entry is environmental: the test resolves `typst`
from `PATH`, not `TYPST_BINARY`. With the pinned binary prepended to `PATH` the
same test and the full suite pass. No application or test assertion was
weakened.

## Cost / duration

Token and cost figures: UNKNOWN (not measured). Durations only where the test
runner printed them.
