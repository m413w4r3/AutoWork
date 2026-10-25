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
| M05-C | — | — | NOT_RUN | pending |
| M05-D | — | — | NOT_RUN | pending |

## Global gates (after integration on local main)

| Gate | Result |
|---|---|
| `make lint-backend` | PASS after M05-A integration (`7e51912a`) — must be rerun at final SHA |
| `make typecheck-backend` | PASS after M05-A integration (`7e51912a`) — must be rerun at final SHA |
| `make test-backend` | PASS at baseline 55fba67d (4904 passed, 2 skipped) — must be rerun at final SHA |
| `make test-integration` (full) | NOT_RUN |
| `make test-workflow` | PASS after M05-A integration (`68aefb2a`): 22 passed, 15 deselected, JUnit guard OK — rerun at final SHA |
| `make test-workflow-full` | NOT_RUN |
| `make test-ci-scripts` | PASS after M05-A integration (`7e51912a`, 5 tests) — rerun at final SHA |
| frontend gates | NOT_RUN (backend-only change expected; confirm) |
| full E2E-0 + E2E-1 in a recreated environment | NOT_RUN |

## Cost / duration

Token and cost figures: UNKNOWN (not measured). Durations only where the test
runner printed them.
