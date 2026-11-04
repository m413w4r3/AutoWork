# M14 disclosure profiles

Configure `ADVISOR_DISCLOSURE_PROFILE` for the backend, worker, and job-recovery
processes. It accepts `standard` (default) or `m14_research_permissive`.

`standard` keeps the existing disclosure checks. The M14 profile treats source
TLP labels, `external_llm_allowed`, sample licence limits, `do_not_submit`, and
sample restriction or revocation labels as audit metadata for Bridge
consultations and VirusTotal reads/downloads. The invocation audit retains the
active profile and original labels.

Both profiles still require an approved G2 envelope, available approved budget,
provider capability, technical feasibility, and one idempotent request per
claim. AutoWork service credentials and signed service URLs are omitted or
redacted before transport. CTI secrets remain evidence.

Check the active value in the application settings as
`Settings.advisor_disclosure_profile`; it is loaded from
`ADVISOR_DISCLOSURE_PROFILE`. Compose passes the same value to backend,
worker, and job-recovery.
