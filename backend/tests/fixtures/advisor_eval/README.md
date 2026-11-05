# Advisor anti-slop corpus

`anti_slop.v2.jsonl` updates unresolved optional-reference cases: readable
advisor text remains usable and partially parsed, with a warning and unresolved
reference retained for audit. The fact remains unanchored for evidence
accounting. Strict rejection of an actually invalid reference remains covered
by `invalid-ref-negative`. `manifest.v1.json` records the active corpus version,
case count, and SHA-256.
