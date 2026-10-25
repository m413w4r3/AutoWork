# M05-R0 brief — read-only map for E2E-0 (no sample)

You are a **read-only reconnaissance agent**. Do not edit repository files, do
not commit, do not run any provider/Bridge/CTI/network call. You may run
existing offline tests (they must not contact external services). Base:
local `main@55fba67d5882c1ae6c4abff4e74c53a24e57649f` in your worktree.

Goal: give the parent a precise, compact implementation map for lot **M05-B —
one continuous PostgreSQL E2E scenario without a sample**, per
`docs/mission05-phase3/ARCHITECTURE_DECISIONS.md` §"E2E-0" and the V3 prompt §6.
Study existing infrastructure before proposing anything new.

## Questions to answer with `file:line` evidence

1. In `backend/tests/integration/test_investigation_vertical_postgres.py`,
   enumerate the reusable seams of the main test (line ranges/helper names):
   disposable PG setup, investigation creation, fake Bridge transport / model
   service, operator-led authorization (G2), observations, finding versions,
   context build/full+paged/cursor, advisor consultation, opinion approval,
   V5/V6 publication, audit export, restart/recovery. State exactly what the
   existing test already asserts and what it does **not**.
2. Which helper/fixture modules already build: fake Bridge/LLM transport with a
   call counter, artifact/blob store (filesystem or MinIO), evidence recorder,
   opinion service, publication renderer, audit export? Give exact import
   paths and constructor signatures.
3. Advisor through the real port: how `InvestigationAdvisorTool` is wired in
   tests with a fake transport (see `test_investigation_advisor_tools.py`,
   e.g. `DurableReservationTransport`), how `prepare()` and
   `ModelConversationService.add_turn` can be exercised without egress, and
   how a terminal refusal, a `partial` answer and an admissible v4 answer are
   injected. Note where v4 parsing/reparse is offline-safe.
4. Real PDF: exact helpers to render V5 then V6 with real Typst 0.15.1 +
   Poppler, reviewer/approver lineage metadata, and `pdftotext` assertions.
   Note required env (`TYPST_BINARY`, fonts) and skip conditions.
5. Run the main vertical test and the analyst-opinion publication test once to
   prove the local environment works. Use:

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst
export TMPDIR=$HOME/.cache/autowork-tmp && mkdir -p "$TMPDIR"
cd backend
TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres \
  uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_investigation_vertical_postgres.py -q --tb=short
```

   and the same for
   `tests/integration/test_analyst_opinion_publication_postgres.py`.
   Report PASS/FAIL/SKIPPED with counts; if a test cannot run, the exact reason.
6. Gap matrix vs E2E-0 requirements: one row per required seam, existing
   coverage reference, missing work, and the minimal file/function to add.
   Then propose **one** continuous driver (new test function or new file) with
   a single investigation id, listing imports and the ordered steps. Flag any
   seam that would be faked and exactly where.

## Deliverable

Final message ≤120 lines, no repository edits, no logs pasted. Use
`path:line` references, verified facts vs inference, exact test commands and
results actually observed, blockers, and the proposed M05-B file scope. If a
required runtime capability is missing, say `BLOCKED_*` with the evidence.
