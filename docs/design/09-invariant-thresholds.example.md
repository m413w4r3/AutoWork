# M3 / P09 — invariant threshold decision

Copy this file to `docs/design/09-invariant-thresholds.md`, replace every `CHOOSE_ME`, and commit it before running P09. These are operator/human policy choices; Codex must not invent them.

```yaml
schema: autowork-m3-invariant-thresholds-v1

goodware:
  suspicious_count: CHOOSE_ME   # integer >= 1
  banal_count: CHOOSE_ME        # integer >= suspicious_count

patterns:
  max_pattern_chars: CHOOSE_ME  # integer >= 1

code_ngram:
  max_mask_ratio: CHOOSE_ME     # decimal in [0, 1]
  min_contiguous_fixed_bytes: CHOOSE_ME  # integer >= 1

likely_packed:
  # Choose one explicit deterministic policy over the M2 PackingSignals.
  # Do not add a model score. Delete unused predicates and define AND/OR semantics exactly.
  operator: CHOOSE_ME            # e.g. ANY or ALL
  max_executable_section_entropy_gte: CHOOSE_ME_OR_NULL
  executable_bytes_per_function_gte: CHOOSE_ME_OR_NULL
  known_packer_marker_hit: CHOOSE_ME  # true/false predicate enabled?
```

Human checklist:

- Values were chosen deliberately for the local evaluation corpus, not copied from a model suggestion.
- `suspicious_count <= banal_count`.
- Mask ratio is between 0 and 1.
- The packing policy is fully deterministic from fields already persisted by M2.
- No threshold is inferred from the twenty proposals of Point 2; Point 2 evaluates the frozen policy rather than silently tuning it mid-run.
