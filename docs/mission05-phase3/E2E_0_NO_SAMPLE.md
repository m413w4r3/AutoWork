# E2E-0 — continuous investigation without a sample

**Verdict: `E2E_NO_SAMPLE_PASS`** under the corrected acceptance contract
(closure code SHA `4111acf3`). The earlier `E2E_NO_SAMPLE_PARTIAL` verdict at
`a30a7c22` is preserved under "Verdict evolution"; the two contradictions it
recorded are resolved as acceptance criteria, not relabelled as passing.

## Verdict evolution

| Date / code SHA | Verdict | Reason |
|---|---|---|
| 2026-10-25, `a30a7c22` | `E2E_NO_SAMPLE_PARTIAL` | Continuous driver passed; real P10 `propose()` and authoritative V4 reparse were labelled NOT_DEMONSTRATED. |
| 2026-10-25, `4111acf3` | `E2E_NO_SAMPLE_PASS` | P10 absence is proven with the real service; a real P10 is proven where its preconditions hold; authoritative V4 reparse is out of the E2E-0 contract while the cutover is blocked and remains covered offline. |

## Corrected acceptance criteria and justification

1. **P10 in a no-sample investigation.** `ProposalConversationService.propose()`
   first requires a bound goodware baseline and then at least one origin Sample
   (`backend/src/cti_app/application/invariant_proposals.py:158,328,359`). A
   no-sample E2E-0 cannot satisfy those preconditions. The driver now asserts
   the durable absence (`samples.list_for_subject() == []`; no
   `investigation_goodware_baselines` binding) and calls the **real**
   `propose()`, expecting the typed first-precondition failure
   `ProposalContractError("The investigation has no goodware baseline
   binding")`, with zero adapter calls, no P10 conversation turn and no P09
   call. The V3 terminal-gate requirement ("a refused P10 creates no P09
   candidate") remains asserted separately through the real response
   processor. A successful no-sample P10 is not a valid requirement: it would
   contradict the P10 application invariant.
   **Coverage elsewhere:** `test_invariant_proposals_postgres.py` proves the
   real P10 with its preconditions satisfied (persisted Sample, imported and
   bound goodware index, real P09 invariant) and proves replay reuses the
   persisted turn with no second send.
2. **Authoritative V4 reparse.** The V3 E2E-0 text requires a v4-admissible
   advisor **response** through the fake transport, not an authoritative V4
   reparse of the consultation archive. The production writer remains
   v2/v3-authoritative and `ADVISOR_CUTOVER_BLOCKED_BY_S3` is unchanged, so
   relabelling that archive as V4 would violate the reparse adapter's own
   contract. The driver keeps the synthetic offline V4 parser +
   registered `advisor.reparse` proof and labels the authoritative archive
   reparse `NOT_APPLICABLE` while the cutover is blocked. Offline V4 coverage is
   retained, not deleted.

Both exclusions are acceptance-contract corrections backed by executed
coverage elsewhere, not label changes.

## Driver

- Test node:
  `backend/tests/integration/test_investigation_e2e_no_sample_postgres.py::test_investigation_no_sample_postgres_continuous_journey`
- Origin: integrated on local `main` at `8da3300c` (candidate `80076e6a`, base
  M05-A `2e7733be`; M05-F `12671c6a` included by main). Closure assertion
  changes integrated at `dd5636e1` on top of `a30a7c22`.
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
| P10 absence (closure) | real `ProposalConversationService.propose()` | no subject Samples, no baseline binding, typed `ProposalContractError` for the first unmet precondition, zero adapter calls, no P10 turn, no P09 call | PASS |
| Observations + finding versions | PG repositories | finding v1 → v2 with `supersedes`; current vs history | PASS |
| Context full + paged | `ResearchContextBuilder.build`, `read_context_section`, `read_context_section_page` | canonical context SHA; each page `context_sha256` equals full SHA; pages equal full section; cursor exhaustion | PASS |
| Advisor blocked | real `advisor.consult` with `external_llm_allowed=false` | typed refusal; `transport.calls == []` | PASS |
| Advisor partial + succeeded | real tool + fake `_ReservationTransport` | 2 sends; archived prompt bytes == captured prompt; SHA matches archive; exact `CONTEXT_BYTES`/`CONTEXT_TOKENS` reservation | PASS |
| Refused P10 → no P09 candidate | real response processor + P09 spy | refused response produces `()` results and zero P09 calls | PASS |
| V4 parse/reparse | synthetic persisted V4 fixture + registered `advisor.reparse` | offline V4 parser and adapter exercised; authoritative archive reparse `NOT_APPLICABLE` (blocked cutover), archive never relabelled | PASS (offline) |
| Human opinion revise/approve/publish | opinion HTTP services | V5 base preserved; V6 augmented with exact reviewer/approver lineage | PASS |
| Real PDF | `PublicationRenderService` + Typst 0.15.1 + Poppler | exact target observation UUID appears exactly once; chronology markers once and in order; actor names not visible | PASS |
| Audit export | real PG snapshot + bundle + resume | foreign investigation rows excluded; target rows retained; resume equals first bundle | PASS |
| Restart/replay | second composition on same PG | `idempotent_replay`, same invocation ID, `NEEDS_REVIEW` preserved, reservation/diagnostics/hashes/versions unchanged, no second send | PASS |

## Exact simulation points

- Bridge/LLM submission: `_ReservationTransport` in the driver (records
  prompts; returns scripted `TurnSubmission`s; never touches the network).
- VT/Shodan: not used at all; G2 budgets set to zero and acquisition disabled.
- The V4 fixture is synthesised test data parsed by the real V4 parser; it does
  not relabel the v3-authoritative consultation archive.

## Command and results

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst
export TMPDIR=/home/nill/.cache/awtmp
export FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst
export TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres
cd backend
uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_investigation_e2e_no_sample_postgres.py -q --tb=short -p no:cacheprovider
```

Observed in the closure pass: the four targeted oracles (`E2E-0`, `E2E-1`,
P10, in-flight recovery) passed together in **8.43 s** at `ca78f1e4`; the final
full integration suite on `4111acf3` passed **351 tests in 151.44 s**,
including this driver. Earlier runs at `a30a7c22`: 1 passed in 4.55 s (parent),
4.00 s (implementer), 4.03 s (reviewer). No external service contacted.

## Independent adversarial replay (M05-D)

The M05-D module was integrated at `70ffb9df` and replayed on the D tree by
the parent (**2 passed**, 7.06 s, both E2E drivers). The final full integration
run at `25de7308` passed all **348** tests, including this driver and the three
adversarial tests. D adds context CAS/egress-guard, sample-policy veto,
terminal-redelivery and real-PDF provenance checks.

## Missing proofs / non-claims

1. **A successful no-sample P10 remains impossible** by application invariant.
   The driver proves the typed absence, not a bypass. The missing-Sample
   snapshot guard is later than the missing-baseline guard and is not directly
   exercised in E2E-0; the driver asserts `subject_samples == []` and the real
   P10 test satisfies both preconditions.
2. **Authoritative V4 reparse of the consultation archive** remains not
   demonstrated and is excluded by the corrected contract while
   `ADVISOR_CUTOVER_BLOCKED_BY_S3` holds. Offline V4 parse/reparse is
   demonstrated on a synthetic persisted V4 fixture.
3. MinIO/S3, real VT/Shodan/Bridge and live CTI remain out of scope and
   `NOT_DEMONSTRATED`.

All other required seams are executed in one continuous driver with linked IDs
and real PostgreSQL/PDF.
