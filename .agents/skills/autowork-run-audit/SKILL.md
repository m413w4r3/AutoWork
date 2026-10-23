---
name: autowork-run-audit
description: Read-only audit of a particular AutoWork investigation or Run12 archive to assess what happened, what evidence exists and what remains unverified.
---

Inputs: run/subject/investigation IDs, exact source/commit SHA, approved
read-only evidence locations and optionally Mission 00 outputs. Start from
`docs/README.md`, `docs/operations/run12-editorial-closure-2026-10-22.md`
and the approved canonical operator status; don't assume private archives
are present.

Compare persisted operations, Bridge dossiers, budget reservations, provider
transport, observation refs and failure statuses. Keep `not_transmitted`,
`not_retrieved`, `partial`, `needs_review`, `unknown` distinct.
No live calls, no write, no reset, no sample execution, no G2 change.
Return factual timeline with IDs/SHA, contradictions, missing evidence
and reproducible read-only next checks. If sources absent, BLOCKED/UNKNOWN,
not a fabricated PASS. Reports are not canonical CTI evidence.
