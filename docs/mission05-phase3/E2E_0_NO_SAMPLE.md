# E2E-0 — continuous investigation without a sample

Status: **PENDING M05-B**. No PASS is written before the integrated driver is
executed on a disposable PostgreSQL instance with a real Typst/Poppler PDF.

## Driver

- Test node: —
- Candidate/base SHA: —
- Single investigation ID: —
- Disposable database: —
- Isolated object store: —
- External effects replaced by fakes: — (exact seam per fake)

## Journey actually traversed

| Step | Application seam | Assertion (SQL/version/SHA/ID) | Status |
|---|---|---|---|
| Synthetic investigation + sources | — | — | NOT_RUN |
| G2 operator-led admission | — | — | NOT_RUN |
| Observations + finding versions | — | — | NOT_RUN |
| Context full + paged + cursor | — | — | NOT_RUN |
| Advisor preparation (pure) | — | — | NOT_RUN |
| Advisor terminal refusal (no send) | — | fake_send_count == 0 | NOT_RUN |
| Advisor `partial` response | — | — | NOT_RUN |
| Advisor admissible v4 response | — | — | NOT_RUN |
| P10 refused → no P09 candidate | — | — | NOT_RUN |
| Human opinion/revision | — | — | NOT_RUN |
| PostgreSQL audit export | — | — | NOT_RUN |
| V5 publication | — | — | NOT_RUN |
| V6 augmentation + reviewer/approver lineage | — | — | NOT_RUN |
| Real Typst/Poppler PDF | — | chronology/evidence assertions | NOT_RUN |
| Restart/replay from RUNNING/uncertain outcome | — | no second call same operation key; `NEEDS_REVIEW` | NOT_RUN |

## Exact simulation points

- —

## Command and result

```sh
# exact command
```

Observed: — (PASS/FAIL/SKIPPED counts, duration only if measured).

## Missing proofs / blockers

- —
