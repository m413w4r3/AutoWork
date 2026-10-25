# E2E-1 — continuous investigation with an inert synthetic sample

**Verdict: `E2E_SYNTHETIC_SAMPLE_PASS`** for the offline inert-sample journey.
Real VirusTotal transport and MinIO/S3 are **NOT_DEMONSTRATED** and are not
claimed; the isolated filesystem object store is the storage backend exercised.
This is not malware analysis and not S3 validation.

**Closure pass (2026-10-25, code SHA `4111acf3`):** this driver is unchanged
and was replayed on the integrated code. The real P10 proof deliberately lives
in a separate focused PostgreSQL test (`test_invariant_proposals_postgres.py`)
so this journey's audit counts, evidence chain and replay assertions are not
rewritten to accommodate a P10 turn. See `E2E_0_NO_SAMPLE.md` for the corrected
E2E-0 contract.

## Driver

- Test node:
  `backend/tests/integration/test_investigation_e2e_synthetic_sample_postgres.py::test_investigation_e2e_synthetic_sample_postgres`
- Integrated on local `main` at `222942e2` (candidate `38d9312d`, base
  `12671c6a` which contains M05-A and M05-F).
- Single investigation ID and one disposable PostgreSQL database; isolated
  `FilesystemBlobStore(tmp_path / "blob-store")`.
- Inert bytes: `SyntheticSampleMaterial.unique()` — an `MZ`-prefixed payload
  with a `NO-EXECUTION` marker, known SHA-256 and length, never executed.
- External effect replaced by a fake: the hash-checking fake VirusTotal port is
  the only download seam. Real application acquisition
  (`HitAcquisitionService` → `VirusTotalSampleAcquisitionService`), real PG
  persistence, real G3, the real publication renderer and Typst/Poppler are
  exercised.

## Journey actually traversed

| Step | Application seam | Assertion | Result |
|---|---|---|---|
| Synthetic investigation + G2 | HTTP start + envelope approval | bounded budgets; downloads=1 | PASS |
| Inert acquisition | real acquisition service + fake VT port | one download; exact SHA/bytes/MIME/length; budget consumed | PASS |
| Quarantine blob | `ProductionArtifactStore` + `FilesystemBlobStore` | bucket `samples-quarantine`, key `samples-quarantine/<sha[:2]>/<sha>`, descriptor SHA/size/MIME, byte read-back | PASS |
| Sample + association + G3 | PG repositories + `SampleQualificationService.qualify` | candidate → confirmed by human actor with evidence refs | PASS |
| Local measurement | fake local-analysis worker + observation writer | sample SHA/length/MIME/measurements; sample→blob→observation→measurement→finding chain | PASS |
| D7 allowed passage | real `advisor.consult` + fake transport | exactly one send; sent bytes equal archived prompt; SHA matches; sample hash in prompt | PASS |
| Veto: TLP RED | persisted sample + advisor | typed `disclosure_restricted` / `POLICY_BLOCKED`; zero send; association trace | PASS |
| Veto: `do_not_submit` | persisted sample + advisor | typed refusal; zero send; restriction source | PASS |
| Veto: `external_llm_allowed=false` | persisted sample + advisor | typed refusal; zero send; restriction source | PASS |
| Veto: owner/source restriction | persisted sample + advisor | typed refusal; zero send; restriction source | PASS |
| Veto: AutoWork credential | persisted observation + advisor | `autowork_credential_or_signed_url_detected`; zero send; marker not echoed | PASS |
| Veto: signed URL | persisted observation linked to a finding + advisor | typed refusal; zero send; URL/marker not echoed | PASS |
| Veto: missing evidence ref | persisted observation + absent UUID | operator `NEEDS_REVIEW`/`invocation_needs_review`; adapter `invalid_input`/`advisor_input_rejected`/`confirmed_non_execution`/`not_submitted`; zero send | PASS |
| Findings/opinion | PG + opinion services | versioned findings, human revise/approve | PASS |
| Audit export | real PG snapshot + bundle + resume | `finding.version_recorded`, `opinion.version_recorded`, `advisor_call.metadata`; exactly-once counts and event order; resume equality; no credential/signed-URL marker in trace | PASS |
| V5 → V6 + PDF | real `PublicationRenderService` + Typst 0.15.1 + Poppler | V5 preserved; V6 augmented; reviewer/approver in lineage, not visible in PDF; inert-sample and measurement content visible | PASS |
| Replay | real acquisition service + renderer re-invoked | no second download; same Sample/association; unchanged consumed budget; same V6 artifact/bytes; render SHA and PDF bytes identical; canonical history unchanged | PASS |

## Exact simulation points

- VirusTotal download: `HashCheckedVirusTotal`-style fake port; no network.
- Local analysis: deterministic fake measurement worker; no execution.
- Advisor transport: the driver's fake transport; no provider/Bridge call.
- Storage: isolated filesystem adapter, not MinIO.

## Command and result

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst
export TMPDIR=/tmp
export FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst
export TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres
cd backend
uv run --no-sync --python 3.12 pytest -m integration \
  tests/integration/test_investigation_e2e_synthetic_sample_postgres.py \
  -q --tb=short -p no:cacheprovider
```

Observed: **1 passed in 5.17 s** on integrated main (parent run);
implementer 4.68 s; independent reviewer 4.86 s. No external service or sample
execution.

Closure replay at `4111acf3`: the four targeted oracles (`E2E-0`, `E2E-1`,
P10, in-flight recovery) passed together in **8.43 s** at `ca78f1e4`; the final
full integration suite passed **351 tests in 151.44 s** on `4111acf3`, this
driver included.

## Independent adversarial replay (M05-D)

The M05-D module was integrated at `70ffb9df` and replayed on the D tree by
the parent (**2 passed**, 7.06 s, both E2E drivers). The final full integration
run at `25de7308` passed all **348** tests, including this driver and the three
adversarial tests. D adds a persisted, confirmed sample veto matrix (TLP RED,
`do_not_submit`, external disclosure disabled, `license_restriction`), a
terminal-redelivery audit/chronology replay and real V5/V6 PDF checks. It does
not exercise MinIO/S3 or the real VirusTotal transport, so those non-claims
stand.

## Missing proofs / non-claims

- **MinIO/S3:** not exercised. The isolated filesystem store is a valid
  isolated test object store but does not prove S3 behavior. `S3_BLOCKED_DESIGN`
  remains unchanged.
- **Real VirusTotal transport:** not exercised; the hash-checking fake port is
  the deliberate acquisition seam.
- **Malware analysis:** the sample is strictly inert; no execution, scanning
  or behavior is claimed.
- **P10/P09:** not part of this journey. A real P10 with satisfied
  preconditions (persisted Sample, imported/bound goodware index, real P09
  invariant, replay without a second send) is proven in
  `backend/tests/integration/test_invariant_proposals_postgres.py`; see
  `E2E_0_NO_SAMPLE.md` for the corrected no-sample contract.
