# M05-B independent review brief — E2E-0 no sample

You are an independent **read-only reviewer**. You did not implement M05-B.
Do not edit, commit, merge or push. Do not contact any external service. Read
`docs/mission05-phase3/_work/REVIEW_PROTOCOL.md` and `M05B_BRIEF.md` first.

## Target

- Base: M05-A commit `2e7733be2c0a636b68cd82fb813d5e118f108380`
  (integrated on main at `b8a10165`).
- Candidate: the M05-B branch HEAD in the B worktree (exact SHA given by the
  parent).
- Main disposable PostgreSQL: `TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres`.
  Typst: `TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst`, `TMPDIR=/tmp`.

## Mandatory checks

1. **One continuous driver**: one test function with one investigation ID and
   one disposable database; it must traverse sources/G2 → observations/finding
   versions → context full+paged → advisor fake transport (blocked refusal,
   partial, succeeded v4) → reparse → P10 refused/no P09 → opinion/audit →
   V5→V6 real PDF → restart/replay `NEEDS_REVIEW`. A collection of separate
   tests is `REVISE`.
2. **No sample**: assert no sample association exists, no VT/Shodan download,
   no sample bytes; a sample path appearing in this scenario is `BLOCKER`.
3. **Advisor oracle**: the fake transport receives exactly the archived prompt
   bytes; `prompt_sha256` matches; a blocked/vetoed path has
   `fake_send_count == 0`; the reservation for the invocation operation key
   has `CONTEXT_BYTES == len(prompt utf8)` and
   `CONTEXT_TOKENS == max(1,(len+3)//4)`. A vacuous `>= 0` check is `REVISE`.
4. **Context oracle**: full and paged reads share the real canonical context
   SHA; page contents equal the full section; cursor exhaustion is proven.
   Tautologies such as `len(sha)==64` do not count as proof.
5. **P10/P09**: a refused P10 response must not reach P09. A bypass using
   `ProposalConversationService(None, None, ...)` + private
   `_process_response_for_p09` is not the real service path; either the real
   `propose()` is driven with a fake conversation service and a counting P09
   registry over PG, or it is explicitly labelled complementary and the
   end-to-end P10 seam is marked `NOT_DEMONSTRATED`.
6. **Audit**: real `read_investigation_audit_snapshot` / `write_audit_bundle`
   over PG with resume semantics; scoped rows and no cross-investigation leak.
7. **PDF**: real `PublicationRenderService` + pinned Typst + `pdftotext`, not a
   stub; V5 preserved, V6 augmented; exact reviewer/approver in lineage
   metadata and not rendered as visible names; chronology appears exactly once
   and in order; evidence reference assertion is exact (no prefix/SHA fallback
   that could match unrelated text).
8. **Restart/replay**: a second composition over the same PG replays the same
   operation key without a second send, returns `idempotent_replay`, and keeps
   `NEEDS_REVIEW`/reservation/diagnostics; hashes/versions unchanged.
9. **Scope/honesty**: only the new test file plus any explicitly declared
   additive helper; no application send/parser/S3/frontend changes; no skipped
   or mocked seam presented as PASS. Verify claims against the commands you
   run.

## Commands

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export UV_CACHE_DIR=/tmp/m05b-review-uv
export TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst
export TMPDIR=/tmp
export TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres
cd <B-worktree>/backend
uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_investigation_e2e_no_sample_postgres.py -q --tb=short -p no:cacheprovider
```

Then spot-check that the original vertical/publication/acquisition tests still
pass (the parent runs the full gates).

## Verdict

`APPROVED` or `REVISE` (≤60 lines) with path:line findings, severity, commands
actually run, and residual risks. Any missing required seam is
`NOT_DEMONSTRATED`, not PASS.
