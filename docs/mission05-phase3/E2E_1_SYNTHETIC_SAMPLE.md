# E2E-1 — continuous investigation with an inert synthetic sample

Status: **PENDING M05-C**. No PASS is written before the integrated driver is
executed on a disposable PostgreSQL instance with a real Typst/Poppler PDF.
An inert sample never validates malware analysis, a live provider or S3.

## Driver

- Test node: —
- Candidate/base SHA: —
- Single investigation ID: —
- Disposable database: —
- Isolated object store / effective bucket/path: —
- Inert bytes SHA-256 / length / MIME: —
- External effects replaced by fakes: — (exact seam per fake)

## Journey actually traversed

| Step | Application seam | Assertion (SQL/version/SHA/ID) | Status |
|---|---|---|---|
| Synthetic investigation + G2 | — | — | NOT_RUN |
| Fake acquisition/import of inert bytes | — | one fake download; SHA/MIME/length | NOT_RUN |
| Quarantine blob in isolated store | — | exact key/bucket/read-back | NOT_RUN |
| Sample + association + G3 | — | — | NOT_RUN |
| Observation + fake local analysis/measurements | — | sample→blob→observation→measurement | NOT_RUN |
| D7 allowed passage | — | one fake send; bytes/SHA equal | NOT_RUN |
| Veto: TLP RED | — | typed refusal; fake_send_count == 0 | NOT_RUN |
| Veto: `do_not_submit` | — | typed refusal; fake_send_count == 0 | NOT_RUN |
| Veto: `external_llm_allowed=false` | — | typed refusal; fake_send_count == 0 | NOT_RUN |
| Veto: source/owner restriction | — | typed refusal; zero send | NOT_RUN |
| Veto: AutoWork credential | — | redaction/refusal; zero send; no echo | NOT_RUN |
| Veto: signed URL/revocation | — | typed refusal; zero send; no echo | NOT_RUN |
| Findings/opinion/audit | — | — | NOT_RUN |
| V5 → V6 + real PDF | — | lineage/chronology | NOT_RUN |
| Replay: no double acquisition/consumption/render | — | no second download; identical hashes | NOT_RUN |

## Exact simulation points

- —

## Command and result

```sh
# exact command
```

Observed: — (PASS/FAIL/SKIPPED counts, duration only if measured).

## Missing proofs / blockers

- MinIO/S3: deliberately unproven; see `SECURITY_REPLAY_AND_S3_GATES.md`.
