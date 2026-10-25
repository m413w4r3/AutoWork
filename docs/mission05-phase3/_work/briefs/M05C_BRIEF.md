# M05-C brief — one continuous E2E scenario WITH inert synthetic sample

## Role and workspace

You are the sole implementer of lot **M05-C**. Work in your own Paseo
worktree, branched from local `main` **after M05-B is integrated** (the parent
supplies the exact base SHA). Commit atomically on your branch; never merge,
rebase or push `main`. Do not edit M05-A or M05-B files.

Read first: root `AGENTS.md`, `backend/AGENTS.md`,
`docs/mission05-phase3/ARCHITECTURE_DECISIONS.md` §"E2E-1",
`docs/mission05-phase3/_work/R1_E2E1_MAP.md` (read-only recon map), and the
M05-B driver once integrated (reuse its patterns, do not modify it).

## Objective (single)

Deliver **one continuous PostgreSQL test driver with strictly inert synthetic
sample bytes** under a single investigation ID:
synthetic investigation → G2 envelope → fake acquisition/import of inert bytes
→ quarantine blob in an isolated object store → Sample + association → G3
human qualification → observations and fake local analysis/measurements with
traceable SHA/length/MIME → D7 Advisor consultation through the existing port
with a fake transport, including an allowed permissive passage **and** each
veto → findings → human opinion/audit → V5 then V6 publication with **real
Typst/Poppler PDF** → replay proving no double acquisition, consumption or
incorrect render, with canonical history and hashes unchanged.

One test function (`test_...e2e_synthetic_sample...`) runs the ordered
journey. Private helpers in the same file and imports of existing helpers are
allowed; separate tests never substitute for the driver.

## Starting map (verify before relying)

- `backend/tests/integration/test_investigation_acquisition_postgres.py`:
  G2 acquisition/G3 decision (`:189`), G1 context handoff (`:294`), same-subject
  reuse (`:494`), refusal/cache (`:679`), operator import (`:784`), local
  analysis/measurements (`:848`). These are separate, not one journey.
- `tests.workflow_emulator.investigation_support`: `SyntheticSampleMaterial`
  (unique inert MZ-prefixed bytes with SHA/IP), `HashCheckedVirusTotal`
  (`download_calls`, hash-verified bytes), `persist_sample_association_fixture`
  (writes `samples-quarantine` blob + Sample + association; its default sample
  is `do_not_submit=True, external_llm_allowed=False`, so a permissive sample
  needs explicit fields), `build_investigation_app`, opinion/publication HTTP
  helpers, `ScriptedTurnTransport`.
- Isolated store: `FilesystemBlobStore(tmp_path / "blob-store")` is the adapter
  already used by every acquisition PG test. Keys are
  `<logical_bucket>/<sha[:2]>/<sha>`; acquisition uses logical bucket
  `samples-quarantine` and MIME `application/octet-stream`. Assert the
  effective root, logical bucket, object key, byte length and SHA, and read the
  bytes back identically. A test-only MinIO is **not required** for this lot;
  do not touch live MinIO. If you attempt one, it must use a unique compose
  project, unique ports, bucket and volume, and prove the effective endpoint
  and bucket; otherwise document that MinIO/S3 remains unproven.
- D7 matrix: existing unit tests in `test_investigation_advisor_tools.py`
  cover TLP RED / `do_not_submit` / `external_llm_allowed=false` (`:4507`,
  `:2203`), source/owner restrictions (`:2203`, `:2388`, `:4228`), AutoWork
  credentials (`:2653`, `:5007`), signed URLs and sample bytes (`:2482`,
  `:2585`), with zero transport calls. Your job is to link each veto to the
  **actual persisted sample/association/finding** in this journey and keep
  `fake_send_count == 0` for every veto with a typed refusal and provenance.

## Required ordered journey and proofs

1. Create investigation + synthetic sources; approve G2 with bounded budget.
2. Acquire/import the inert sample through the fake provider port only:
   assert one fake download, exact SHA-256/MIME/length, a `samples-quarantine`
   blob at the exact isolated key, and bytes read back identical.
3. Persist Sample + association; G3 human qualification with evidence refs.
4. Persist observations and a fake local-analysis result/measurements citing
   the sample hash; assert provenance chain sample hash → blob → observation →
   measurement.
5. D7 Advisor consultation with the fake transport:
   - allowed passage under permissive policy: exactly one fake send, and the
     bytes received equal the archived/prepared UTF-8 bytes (SHA equal);
   - for each veto (TLP RED, `do_not_submit`, `external_llm_allowed=false`,
     source/owner restriction, AutoWork credential, signed URL/revocation,
     completeness/refs conflict where reachable): typed refusal, no
     submission, `fake_send_count == 0`, provenance retained; never echo a
     secret or signed URL.
6. Findings (current vs historical if relevant), human opinion, PostgreSQL
   audit export, V5 publication then V6 augmentation with exact
   reviewer/approver lineage; real Typst/Poppler PDF with chronology/evidence
   assertions (reuse M05-B's helpers/patterns without editing that file).
7. Replay acquisition and publication on the same disposable PG: no second
   fake download, no duplicate sample/association/consumption, no duplicate
   canonical chronology, identical hashes and versions.

## Hard constraints

- Bytes are strictly inert (no executable behaviour, no real malware); never
  execute, scan or upload them. No external provider/Bridge/LLM/VT/Shodan/CTI
  call; only loopback PostgreSQL. No live MinIO/DB, no `.env`/secret read, no
  migration, no deployment, no S3 work, no v4 cutover.
- Do not edit `backend/src` unless a real missing seam blocks the journey; then
  STOP and report. Prefer test-only wiring. Never weaken a policy or skip a
  veto to make the test pass.
- One investigation ID, one disposable DB, real SQL/version/SHA assertions.
  An inert sample proves nothing about malware analysis or S3; do not imply it.

## Suggested file scope

- New `backend/tests/integration/test_investigation_e2e_synthetic_sample_postgres.py`.
- Optional tiny additive helper in `backend/tests/workflow_emulator/investigation_support.py`,
  reported explicitly; never change existing semantics.
- No M05-A/B files, no application send/parser code.

## Environment and commands

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export UV_CACHE_DIR=/tmp/autowork-m05c-uv
export TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst
export TMPDIR=/tmp
export TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres
cd backend
uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_investigation_e2e_synthetic_sample_postgres.py -q --tb=short
```

Also rerun the driver plus `test_investigation_acquisition_postgres.py`,
`test_investigation_vertical_postgres.py` and the M05-B driver to prove no
cross-lot breakage, then ruff check/format on the new file.

## Deliverable (final message, ≤80 lines)

- Base/commit SHAs, clean status, files changed.
- Ordered journey with `path:line` anchors; exact fake points and zero-send
  evidence per veto.
- Object store effective path/bucket/key and SHA/length/MIME proofs.
- G3/opinion/audit/PDF lineage proofs; replay no-duplication proofs.
- Exact commands + observed PASS/FAIL/SKIPPED; deviations, blockers, residual
  risks. Never report an unexecuted veto or PDF as PASS.
