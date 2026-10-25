# M05-B brief — one continuous E2E scenario WITHOUT a sample

## Role and workspace

You are the sole implementer of lot **M05-B**. Work in your Paseo worktree,
branched from local `main` **after M05-A is integrated** (the parent gives you
the exact base SHA). Commit atomically on your branch; do not merge, rebase or
push `main`. Do not touch M05-A's application files or any parser/send-path/
S3 code.

Read first: root `AGENTS.md`, `backend/AGENTS.md`,
`docs/mission05-phase3/ARCHITECTURE_DECISIONS.md` §"E2E-0",
`docs/mission05-phase3/_work/briefs/M05R0_E2E0_MAP.md` (reconnaissance map,
written by a read-only Luna agent), and the existing test helpers you reuse.

## Objective (single)

Deliver **one continuous PostgreSQL test driver, with no sample acquisition**,
that traverses the real application seams under a single investigation ID:
synthetic sources/admission → observations and finding versions → context full
+ paged → Advisor consultation through the existing port with a fake transport
(preparation, terminal refusal, `partial`, admissible v4 answer; zero egress)
→ P10 `refused` creates no P09 candidate → human opinion/revision → PostgreSQL
audit export → V5 publication then V6 augmentation with exact reviewer/approver
→ **real Typst/Poppler PDF** → restart/replay from `RUNNING` or an uncertain
external outcome → `NEEDS_REVIEW` with no second call on the same operation
key. All assertions must use the same disposable PG database and the same
investigation/version/blob IDs.

A pile of separately named tests is NOT acceptable. The deliverable is one
test function (`test_...e2e_no_sample...`) that runs the ordered journey. You
may use additional private helpers in the same file and import existing
helpers; auxiliary tests are allowed but never substitute for the driver.

## Starting map (verify before relying)

- Existing scaffold: `backend/tests/integration/test_investigation_vertical_postgres.py`
  main test at `:115`. It already does G2, observations/findings, opinion and
  V5→V6, but it **acquires a synthetic sample** and lacks context, Advisor,
  audit export, real PDF and restart.
- Reuse `tests.workflow_emulator.investigation_support`:
  `seed_verified_article` (`include_seed=False`), `start_investigation_http`,
  `approve_envelope_http`, `run_http_job`, `compose_opinion_http`,
  `approve_opinion_http`, `revise_opinion_http`, `publish_opinion_http`,
  `build_investigation_app`, `ScriptedTurnTransport` (`submission_count`,
  `prompts`), `persisted...` helpers where relevant.
- Context: `ResearchContextBuilder`, `read_context_section`,
  `read_context_section_page`, `context_pagination`; reuse
  `test_operator_context_summary_postgres.py` semantics.
- Advisor: wire `InvestigationAdvisorTool` with a **fake conversation
  transport** (see `test_investigation_advisor_tools.py`, e.g.
  `DurableReservationTransport`, and `ModelConversationService.add_turn`). The
  fake must count sends and receive exactly the archived prompt bytes. Inject:
  a blocked policy/terminal refusal (no send), a `partial` compact answer, and
  an admissible v4 compact answer (no JSON requirement). A refused P10 must
  produce no P09 candidate. Reparse only from persisted bytes; no new send.
- Audit: `InvestigationAuditExportService` +
  `test_investigation_audit_export_postgres.py` snapshot/resume semantics.
- Real PDF: reuse the real Typst/Poppler helpers from
  `test_analyst_opinion_publication_postgres.py` /
  `test_protocol_diagrams_typst.py` (`_typst_binary`, `TypstRenderer`,
  `TypstSubprocessCompiler`, `pdftotext`). The PDF must be produced by the
  real publication path, not a stub. Assert:
  - V5 base artifact preserved and V6 augmented artifact;
  - exact reviewer/approver in lineage metadata, not automatically rendered as
    visible names;
  - canonical chronology appears exactly once and in order in extracted text;
  - evidence references/hashes visible as required.
- Restart: use a second service/composition instance over the same disposable
  PG. Simulate a `RUNNING`/uncertain external outcome. Assert the reservation
  and diagnostics are preserved, no second submission for the same operation
  key, and `NEEDS_REVIEW` unless a canonical bounded non-submission proof
  exists. Statuses/models/hashes must be identical after the restart.

## Hard constraints

- **No sample**: no sample bytes, no VT/Shodan download, no acquisition import;
  any pre-existing sample path must be absent from this scenario.
- No real provider/Bridge/LLM/VT/Shodan/CTI/network call. The only network is
  loopback PostgreSQL on `127.0.0.1:55432`; all external effects are fakes. For
  each refusal/veto assert `fake_send_count == 0`; assert the fake transport is
  the only submit port and no external HTTP client is used.
- No live PostgreSQL/MinIO, no `.env`/secret read, no migration, no deploy, no
  S3 work, no v4 cutover.
- Do not edit `backend/src` unless a **real missing seam** blocks the E2E; then
  STOP and report the blocker instead of silently changing application
  behaviour. Prefer test-only wiring.
- One investigation ID throughout; one disposable database; real SQL/version/
  SHA assertions. Do not claim PASS for anything not executed.

## Suggested file scope

- New `backend/tests/integration/test_investigation_e2e_no_sample_postgres.py`
  (preferred; self-contained driver).
- If reuse requires a tiny helper extraction in
  `backend/tests/workflow_emulator/investigation_support.py`, keep it additive
  and report it; never change existing helper semantics.
- Tests/fixtures only. No advisor/parser/send-path files.

## Environment and commands

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export UV_CACHE_DIR=/tmp/autowork-m05b-uv
export TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst
export TMPDIR=/tmp
export TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres
cd backend
uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_investigation_e2e_no_sample_postgres.py -q --tb=short
```

Also run the extended test plus the reused originals once:
`test_investigation_vertical_postgres.py`, `test_analyst_opinion_publication_postgres.py`,
`test_investigator_audit_export_postgres.py` (adapt exact name if different),
`test_operator_context_summary_postgres.py`. Then ruff check/format on the new
file.

The parent runs the final full gates; you must still prove the driver itself
passes and report exact counts/durations.

## Deliverable (final message, ≤80 lines)

- Base SHA, branch commit SHA, clean status, files changed.
- The ordered journey actually executed, with `path:line` anchors.
- Exactly where each external effect is faked and the zero-send evidence.
- SQL/version/SHA/ID assertions, context full-vs-paged SHA, cursor result,
  P10/P09 proof, restart/`NEEDS_REVIEW` proof, PDF and audit proof.
- Exact commands + observed PASS/FAIL/SKIPPED counts; deviations, blockers,
  residual risks. Never report an unexecuted seam as PASS.
