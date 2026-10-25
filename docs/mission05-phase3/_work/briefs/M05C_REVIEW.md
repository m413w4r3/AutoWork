# M05-C independent review brief — E2E-1 inert synthetic sample

You are an independent **read-only reviewer**. You did not implement M05-C.
Do not edit, commit, merge or push. Do not contact any external service, and
never download or execute a sample. Read `docs/mission05-phase3/_work/REVIEW_PROTOCOL.md`
and `M05C_BRIEF.md` first.

## Target

- Base: main `73ea620f` (contains M05-A).
- Candidate: the M05-C branch HEAD in the C worktree (exact SHA from parent).
- Main disposable PostgreSQL: `TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres`.
  Typst: `TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst`, `TMPDIR=/tmp`.

## Mandatory checks

1. **One continuous driver**: one test function, one investigation ID, one
   disposable DB; G2 → fake acquisition/import → quarantine blob → Sample +
   association → G3 → observation/measurements → D7 advisor → findings →
   opinion/audit → V5→V6 real PDF → replay. Separate tests do not substitute.
2. **Inert bytes and storage**: exactly one fake download; exact SHA-256,
   MIME, length; bytes read back identically; `FilesystemBlobStore(tmp_path)`
   effective root and `samples-quarantine/<sha[:2]>/<sha>` key asserted; no
   live MinIO/DB. A test-only MinIO, if used, must assert its effective
   endpoint/bucket and use a distinct project/ports/volume. State explicitly
   that MinIO/S3 remains unproven if filesystem storage is used.
3. **G2/G3 and provenance**: association/qualification persisted; the allowed
   D7 passage uses a permissive sample and produces exactly one fake send whose
   received bytes equal the archived prompt bytes and SHA.
4. **Veto matrix**: TLP RED, `do_not_submit`, `external_llm_allowed=false`,
   source/owner restriction, AutoWork credential, signed URL/revocation, and
   completeness/refs conflict where reachable. Each must produce a typed
   refusal, `fake_send_count == 0`, no external client, retained provenance,
   and no secret/signed-URL echo. A veto tested only through a pure unit call,
   not the persistent sample/association, is `NOT_DEMONSTRATED`.
5. **Measurements/provenance**: fake local analysis cites the exact sample
   hash; chain sample→blob→observation→measurement→finding→opinion→PDF is
   asserted with IDs/SHA.
6. **PDF/audit**: real Typst/Poppler, V5 preserved, V6 augmented, exact
   reviewer/approver in lineage and not visible in the PDF, chronology exactly
   once/in order.
7. **Replay**: no second fake download, no duplicate sample/association/
   consumption, no duplicated chronology, identical hashes/versions.
8. **Scope/honesty**: only the new test file; no shared helper or application
   edits unless explicitly declared and justified; no sample execution; no
   skipped/mocked seam presented as PASS.

## Commands

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export UV_CACHE_DIR=/tmp/m05c-review-uv
export TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst
export TMPDIR=/tmp
export TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres
cd <C-worktree>/backend
uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_investigation_e2e_synthetic_sample_postgres.py -q --tb=short -p no:cacheprovider
```

## Verdict

`APPROVED` or `REVISE` (≤60 lines) with path:line findings, severity, commands
actually run, residual risks. An inert sample never validates malware analysis
or S3; do not accept language implying otherwise.
