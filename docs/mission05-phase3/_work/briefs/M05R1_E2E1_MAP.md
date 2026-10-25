# M05-R1 brief — read-only map for E2E-1 (inert synthetic sample)

You are a **read-only reconnaissance agent**. Do not edit repository files, do
not commit, do not run any provider/Bridge/CTI/network call, do not download or
execute any sample. You may run existing offline tests. Base: local
`main@55fba67d5882c1ae6c4abff4e74c53a24e57649f` in your worktree.

Goal: give the parent a precise, compact implementation map for lot **M05-C —
one continuous PostgreSQL E2E scenario with strictly inert synthetic sample
bytes**, per `docs/mission05-phase3/ARCHITECTURE_DECISIONS.md` §"E2E-1" and V3
§7.

## Questions to answer with `file:line` evidence

1. In `backend/tests/integration/test_investigation_acquisition_postgres.py`,
   enumerate reusable seams: G2 acquisition, import, quarantine blob, Sample,
   association, observation, fake local analysis/measurements, G3 decisions,
   sample reuse. Give helper names/lines and what each test asserts.
2. Object storage in tests: how is `BlobStore` implemented in PG tests
   (`FilesystemBlobStore(tmp_path/...)` vs MinIO)? Can an isolated
   endpoint/bucket/volume test-only MinIO be launched from `compose.yaml`
   (`minio`/`minio-init` services), and is it actually needed? Report the
   smallest isolated-store design and exact effective-path/bucket assertions.
3. D7 veto matrix: locate existing tests/production paths for CTI evidence
   disclosure with raw/hex/Base64/association payloads and each veto — TLP RED,
   `do_not_submit`, `external_llm_allowed=false`, source/owner restriction,
   AutoWork credential, signed URL/revocation. For each, state the test
   reference, whether it asserts zero fake transport calls, and what an
   integrated scenario must add. See `test_investigation_advisor_tools.py`,
   `test_content_pivot_advisor_prevalidation.py`,
   `test_advisor_consultation.py`, `advisor_dossier.py`,
   `publication_secret_guard.py`.
4. Inert bytes fixture: existing conventions for deterministic synthetic bytes
   with known SHA-256/MIME/length, and how to bind them as quarantine blob +
   sample + association without touching a live store. Note any existing
   fixture (`tests/fixtures/`, builders in acquisition tests).
5. Opinion/audit/PDF: confirm the same reusable helpers as E2E-0 (opinion
   approval, V5/V6 real PDF Typst/Poppler, audit export) and any sample-specific
   provenance assertions (sample hash → observation → finding → opinion → PDF).
6. Run the acquisition PG test once to prove the environment:

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst
export TMPDIR=$HOME/.cache/autowork-tmp && mkdir -p "$TMPDIR"
cd backend
TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres \
  uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_investigation_acquisition_postgres.py -q --tb=short
```

   Report PASS/FAIL/SKIPPED with counts and the exact reason for any failure.
7. Gap matrix vs E2E-1 requirements and a proposed **one** continuous driver
   (new file/function, single investigation id, ordered steps), flagging every
   point where an external effect is replaced by a fake. Explicitly state
   whether `BLOCKED_STORAGE_TEST` applies.

## Deliverable

Final message ≤120 lines, no repository edits, no logs pasted. Use
`path:line` references, verified facts vs inference, exact commands/results
observed, blockers, proposed M05-C file scope. Do not overstate: an inert sample
never validates malware analysis or S3.
