# M02-S2-P10 Review

TASK_ID: `M02-S2-REVIEW`\
Base: `e4abba495d9f5fdc296e2ba80b65442855625474`\
Reviewed HEAD: `4a13c5d40b60b0afebb75b8ea9b41d80205161e`\
Verdict: **REVISE**

## Findings (ranked)

1. **HIGH — VERIFIED: no-cap widens external disclosure past an incomplete P10 gate.** The changed renderer now accepts arbitrarily large prompts ([invariant_proposals.py](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:939)). P10 routes and submits using only `policy.external_llm_allowed` ([same file](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:241)); `derived_policy` separately carries `do_not_submit` and TLP without folding them into that flag ([classification.py](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/domain/classification.py:39)). With `external_llm_allowed=True` and `do_not_submit=True` (or TLP RED), prompts above the former 96,000-byte refusal can now be sent externally. Enforce the complete disclosure policy before transport.

2. **HIGH — VERIFIED: credential-like URL query is rendered unchanged.** `_redact_url` does not recognize `auth=` ([invariant_proposals.py](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2284)); report-claim fields are rendered directly after normalization ([same file](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:1108)). An offline renderer check confirmed `https://example.invalid/path?auth=FIXTURE_AUTH_TOKEN_123` survives in the prompt. Redact credential-bearing query parameters before rendering.

3. **MEDIUM — VERIFIED: signed-URL redaction erases neighboring evidence.** `_redact_url` replaces the entire string when it finds a signed-URL marker ([invariant_proposals.py](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2288)). The offline renderer check confirmed surrounding `CVE-TEST-123` and family text disappear with a `sig=` URL. Replace only the URL substring so unrelated evidence remains.

4. **INFERENCE — reference digests can change; v4 separation makes this acceptable for new turns.** The six immutable reference names remain unchanged ([invariant_proposals.py](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:542), [invariant_proposals.py](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/domain/invariant_proposals.py:287)). `_sha256_json` canonicalizes through `_bounded_json` ([invariant_proposals.py](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2313)); removal of its 256-item and 2,048-character truncation changes corpus/feature reference hashes when old limits were exceeded. Prompt version 4 changes keys from version 3, so new complete evidence gets a distinct key. Existing v3 snapshot replay through the service is **UNKNOWN**; the inspected tests do not cover legacy replay.

## Verified checks

- The 96,000-byte refusal and old context/string/candidate limits are removed; targeted search found no replacement P10 prompt byte/MB guard in the renderer or conversation service. Tests render through 3 MiB + 17 bytes.
- `_normalize_context`/`_bounded_json` retain complete lists, mappings, strings, offsets and addresses. Bytes are reversibly hex-encoded. The depth `>8` sentinel remains; timestamps and recognized secrets/URLs are still normalized/redacted. Deep values beyond that guard remain intentionally replaced by `[DEPTH_LIMIT]`.
- Equal snapshots render/key deterministically; tests verify prompt version 3 and 4 keys differ. All six reference names are asserted by the existing test.
- The prompt instruction “Do not include raw bytes…” constrains model output; it does not block permitted input evidence or change the transport contract. **UNKNOWN:** any byte-valued report-claim payload would be sent as hex if rendered; no production sample-byte producer was established in this review.
- `_select_candidate_records` only sorts/copies ([invariant_proposals.py](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:1953)); candidates were already sorted and `_measure_candidate` copies them. It can be removed. `_bounded_json` is now a misleading name; consider `_normalize_json_value`. Keep `_normalize_context` as a useful intent boundary.
- Branch diff contains only the P10 module and its tests. The former `<=96_000` assertion was replaced by above-limit preservation cases; no unrelated test weakening found.

## Checks run

- Required: `PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest tests/test_invariant_proposals.py tests/test_bridge_investigator.py tests/test_research_context_builder.py -q --tb=short` — **129 passed in 2.92s**.
- Offline synthetic renderer checks confirmed the `auth=` leak and loss of nonsecret text adjacent to signed URLs. No live provider or service was called.
- `git diff --check e4abba495d9f5fdc296e2ba80b65442855625474..HEAD` — passed.
