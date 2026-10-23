---
name: autowork-bridge-dossier-review
description: Verify a proposed Bridge consultation dossier has the claim passages, contradictions, versions and source references required before any authorized submission.
---

Inputs: `consultation_type`, exact claim/question, source and dossier hashes,
required evidence manifest, current G2/TLP/provider policy and preview
of the **effective** wire payload. Review read-only.

Inspect `advisor_dossier.py`, `advisor_wire.py` and contracts only as needed.
Check required/present/missing/retrievable/intentionally_omitted; aliases,
versions, spans, units, contradictions, truncation and `advisor-wire.v1`
compatibility. A retrievable but unsent fact is **not** unavailable.
Never let a model self-certify semantic completeness.

Output `READY` or `NOT_READY` + exact missing references/SHA and
safe retrieval instructions. No live Bridge submission, no disclosure
override, no invented token/cost measurements.
