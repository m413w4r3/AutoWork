# E2E-0 — continuous investigation without a sample

**Verdict: `E2E_NO_SAMPLE_PARTIAL`.** The continuous driver is integrated and
passes, but two V3-mandated seams are **NOT_DEMONSTRATED**. This is not
`E2E_NO_SAMPLE_PASS`.

## Driver

- Test node:
  `backend/tests/integration/test_investigation_e2e_no_sample_postgres.py::test_investigation_no_sample_postgres_continuous_journey`
- Integrated on local `main` at `8da3300c` (candidate `80076e6a`, base
  M05-A `2e7733be`; M05-F `12671c6a` included by main).
- Single investigation ID and one disposable PostgreSQL database created by
  the `uow_factory` fixture; isolated `FilesystemBlobStore(tmp_path)`.
- External effects replaced by fakes: the Bridge/LLM `_ReservationTransport`
  records each prompt and returns scripted partial/succeeded/uncertain
  submissions; VT/Shodan are not called (acquisition disabled, budgets zero).
  PostgreSQL, the audit snapshot/bundle, the publication renderer and
  Typst/Poppler are real.
- No sample: `sample_associations == ()`, no `vt.download_sample`, no sample
  bytes.

## Journey actually traversed

| Step | Application seam | Assertion | Result |
|---|---|---|---|
| Synthetic article + operator-led investigation | HTTP API start | investigation/state persisted | PASS |
| G2 denied then approved | envelope approval | denied path blocks Advisor; approved envelope bounds budgets | PASS |
| Observations + finding versions | PG repositories | finding v1 → v2 with `supersedes`; current vs history | PASS |
| Context full + paged | `ResearchContextBuilder.build`, `read_context_section`, `read_context_section_page` | canonical context SHA; each page `context_sha256` equals full SHA; pages equal full section; cursor exhaustion | PASS |
| Advisor blocked | real `advisor.consult` with `external_llm_allowed=false` | typed refusal; `transport.calls == []` | PASS |
| Advisor partial + succeeded | real tool + fake `_ReservationTransport` | 2 sends; archived prompt bytes == captured prompt; SHA matches archive; exact `CONTEXT_BYTES`/`CONTEXT_TOKENS` reservation | PASS |
| P10 refused → no P09 candidate | complementary terminal-gate check | refused response produces `()` results and zero P09 calls | **COMPLEMENTARY ONLY — NOT end-to-end** |
| V4 parse/reparse | test-only synthetic V4 fixture + registered `advisor.reparse` | reparse exercised; authoritative archive is explicitly non-V4 | **test-only fixture; authoritative archive NOT_DEMONSTRATED** |
| Human opinion revise/approve/publish | opinion HTTP services | V5 base preserved; V6 augmented with exact reviewer/approver lineage | PASS |
| Real PDF | `PublicationRenderService` + Typst 0.15.1 + Poppler | exact target observation UUID appears exactly once; chronology markers once and in order; actor names not visible | PASS |
| Audit export | real PG snapshot + bundle + resume | foreign investigation rows excluded; target rows retained; resume equals first bundle | PASS |
| Restart/replay | second composition on same PG | `idempotent_replay`, same invocation ID, `NEEDS_REVIEW` preserved, reservation/diagnostics/hashes/versions unchanged, no second send | PASS |

## Exact simulation points

- Bridge/LLM submission: `_ReservationTransport` in the driver (records
  prompts; returns scripted `TurnSubmission`s; never touches the network).
- VT/Shodan: not used at all; G2 budgets set to zero and acquisition disabled.
- The V4 reparse fixture is synthesised test data parsed by the real v4 parser;
  it does not relabel the v3-authoritative consultation archive.

## Command and result

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst
export TMPDIR=/tmp
export FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst
export TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres
cd backend
uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_investigation_e2e_no_sample_postgres.py -q --tb=short -p no:cacheprovider
```

Observed: **1 passed in 4.55 s** on integrated main (parent run);
implementer 4.00 s; independent reviewer 4.03 s. No external service contacted.

## Missing proofs / blockers

1. **Real P10 `propose()`** requires retained origin Samples
   (`invariant_proposals.py`), which the V3 E2E-0 "without a sample" scenario
   forbids. The driver contains only a complementary terminal-gate check.
   This is a direct contradiction between two V3 requirements; it is
   documented, not papered over.
2. **Authoritative V4 reparse from the consultation archive**: the
   v3-authoritative writer persists v2/v3 metadata, and the reparse adapter
   correctly refuses to relabel it. The test exercises v4 parse/reparse on a
   separate synthetic fixture only.

These two items are why the verdict is `PARTIAL`. All other required seams are
executed in one continuous driver with linked IDs and real PostgreSQL/PDF.
