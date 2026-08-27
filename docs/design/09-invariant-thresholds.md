# M3 / P09 — invariant threshold decision

```yaml
schema: autowork-m3-invariant-thresholds-v1

goodware:
  suspicious_count: 3
  banal_count: 10

patterns:
  max_pattern_chars: 96

code_ngram:
  max_mask_ratio: 0.20
  min_contiguous_fixed_bytes: 6

likely_packed:
  operator: ALL
  max_executable_section_entropy_gte: 7.2
  executable_bytes_per_function_gte: 1200
  known_packer_marker_hit: false
```
