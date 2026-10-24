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

## Re-review after d298d181

Reviewed fix `d298d181` merged into worktree HEAD `3ef3de399f8400674961d85376ff4630056ebb92`.
Verdict: **REVISE**.

- **VERIFIED — prior finding 1 closed:** `_external_disclosure_allowed` now requires `external_llm_allowed`, rejects `do_not_submit` and TLP RED before conversation selection and `add_turn` ([invariant_proposals.py:162](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:162), [invariant_proposals.py:2285](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2285)). Service tests assert the local route for DNT and RED ([test_invariant_proposals.py:974](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/tests/test_invariant_proposals.py:974)); they exercise `propose` with a fake conversation adapter, not a real provider.
- **VERIFIED — prior findings 2–3 closed for URL inputs:** URL userinfo and matched credential query values are redacted in place, preserving neighboring text and ordinary `q=` values ([invariant_proposals.py:2290](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2290)). Tests cover service routing, the URL-aware renderer path through `_normalize_context` and `_render_p10_prompt`, and adjacency ([test_invariant_proposals.py:993](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/tests/test_invariant_proposals.py:993), [test_invariant_proposals.py:1017](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/tests/test_invariant_proposals.py:1017)).
- **HIGH — VERIFIED, open secret leak:** non-URL `auth=FIXTURE_AUTH_123`, `authorization: Basic ...`, and `credential=...` survive normalization/rendering; URL `client_secret=...` also misses the exact-name query allowlist. `_redact_text` omits these labels and `_p10_text` emits claim strings directly ([invariant_proposals.py:1133](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:1133), [invariant_proposals.py:2306](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2306)). Extend redaction and add renderer-path cases before approval.
- **LOW — VERIFIED, benign URL metadata over-redacted:** `x-amz-[^=&]*` also removes non-secret fields such as `x-amz-algorithm=AWS4-HMAC-SHA256` ([invariant_proposals.py:2292](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2292)); ordinary `q=` remains. Narrow the matched signing credentials if this metadata is evidence.
- **VERIFIED behavior / INFERENCE on policy:** AMBER and AMBER+STRICT route externally when the explicit external flag is true and DNT is false; RED is blocked. This matches the documented `external_llm_allowed` routing contract ([pipeline policy](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/docs/pipeline_cti_mensuel_composite_v3.md:102), [gateway policy](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/docs/model_gateway.md:78)); no stricter AMBER rule was found. With external disclosure denied, the service selects its configured local provider/transport ([invariant_proposals.py:736](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:736)); QWEN is documented as local ([model_gateway.md:78](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/docs/model_gateway.md:78)). Actual runtime transport was not exercised.
- **VERIFIED:** no replacement P10 prompt-size refusal found; the preservation test includes 3 MiB + 17 bytes ([test_invariant_proposals.py:549](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/tests/test_invariant_proposals.py:549)).

### Re-review checks

- Required: `PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest tests/test_invariant_proposals.py tests/test_bridge_investigator.py tests/test_research_context_builder.py -q --tb=short` — **133 passed in 2.46s**.
- Offline synthetic renderer checks confirmed the remaining secret paths and `x-amz-algorithm` over-redaction. No provider or live runtime was called.
- `git diff --check 4a13c5d4..d298d181` — passed.

### Third pass after 44c1bfac

Reviewed `44c1bfac` merged at worktree HEAD `fc2c1eb3961f2b186b0ca3f1e4ee68c1e6d32580`.
Verdict: **REVISE**.

- **VERIFIED — covered residuals closed:** flat text `auth=`, Basic authorization, `credential=`, `client_secret=`, and the tested HTTPS credential parameters are redacted. Benign prose (“Auth methodology”, “credential theft”) and `x-amz-algorithm` survive; uppercase labels and whitespace before `:` are redacted ([invariant_proposals.py:2292](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2292), [test_invariant_proposals.py:1029](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/tests/test_invariant_proposals.py:1029)).
- **HIGH — VERIFIED, still in S2 scope:** synthetic `_normalize_context` → `_render_p10_prompt` checks show secrets survive when under a structured `auth` mapping key, in `auth_token=`, in percent-encoded `?%61uth=`, in username-only URL userinfo, in `#id_token=`, and in a scheme-less `www...?...sig=` URL. Mapping values are normalized without considering their keys ([invariant_proposals.py:2221](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2221)); generic claim rendering then emits key/value pairs ([invariant_proposals.py:1123](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:1123)). URL handling requires an HTTP(S) scheme and literal matched parameter names ([invariant_proposals.py:2291](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2291)). Add key-aware normalization and cover these bypasses before S2 approval; these remain P10 disclosure-path defects, not separate cleanup.
- **VERIFIED:** the updated tests add cases without weakening the prior assertions. Their render-path fixture covers flat claim text and HTTPS query credentials but not the structured/encoded/fragment cases above ([test_invariant_proposals.py:1049](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/tests/test_invariant_proposals.py:1049)). No provider or live runtime was called.

### Third-pass checks

- Required: `PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest tests/test_invariant_proposals.py tests/test_bridge_investigator.py tests/test_research_context_builder.py -q --tb=short` — **134 passed in 2.13s**.
- Offline synthetic renderer checks confirmed the remaining bypasses; no benign-prose over-redaction was observed.

## Final verification after b344a185

Reviewed `b344a185` merged at worktree HEAD `1107f12c66843ff170106809204d853b67fda568`.
Verdict: **REVISE**; the redaction loop is **not closed**.

- **VERIFIED — prior findings closed for tested forms:** structured sensitive mapping keys redact during normalization ([invariant_proposals.py:2222](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2222), [test_invariant_proposals.py:1070](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/tests/test_invariant_proposals.py:1070)). URL checks cover percent-encoded query names, fragments, scheme-less signed URLs, and username-only userinfo ([test_invariant_proposals.py:1027](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/tests/test_invariant_proposals.py:1027)). Representative mixed-case/separator text labels redact; `token_count`, `author`, and `client_id` controls remain intact.
- **HIGH — VERIFIED residual, general free-text class:** quoted JSON credentials in claim text still reach the rendered prompt. Synthetic `_normalize_context` → `_render_p10_prompt` check: `{"auth": "FIXTURE_JSON_AUTH"}` preserves the fixture secret. `_TEXT_ASSIGNMENT` does not accept the quote after the key, so its matcher misses this common JSON-text form ([invariant_proposals.py:2301](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2301), [invariant_proposals.py:2361](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2361)). This is an in-scope P10 disclosure defect; add quoted assignment handling and renderer-path coverage before approval.
- **VERIFIED:** representative `AuTh_ToKen`, `X-Amz-Security-Token`, and `client-Secret` values redact; benign `token_count=5`, `client_id=client-123`, and `author=Jane` remain. No provider or live runtime was called.

### Final verification checks

- Required: `PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest tests/test_invariant_proposals.py tests/test_bridge_investigator.py tests/test_research_context_builder.py -q --tb=short` — **135 passed in 2.31s**.
- Offline synthetic renderer check confirmed the quoted-JSON residual; `git diff --check` passed.

## Last check after 1246b25f

Reviewed `1246b25f` merged at worktree HEAD `71753e011dd3f0676bf1d7bb0e39106bcb7182db`.
Verdict: **REVISE** for one documented residual; close this bounded review loop: **YES**.

- **VERIFIED — ordinary quoted-JSON finding closed:** double-quoted JSON, single-quoted JSON-like text, and spacing around the label/delimiter redact through `_normalize_context` → `_render_p10_prompt`; benign `token_count` remains. Existing structured-key and URL tests still pass ([test_invariant_proposals.py:1045](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/tests/test_invariant_proposals.py:1045), [test_invariant_proposals.py:1094](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/tests/test_invariant_proposals.py:1094)).
- **VERIFIED residual — category (b), escaped serialization:** `{"auth": "FIXTURE_ESCAPED_AUTH"}` survives the actual render path; `_TEXT_ASSIGNMENT` accepts quote delimiters but not backslash-escaped quotes ([invariant_proposals.py:2301](/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s2-p10-review/backend/src/cti_app/application/invariant_proposals.py:2301)). This is an escaped-serialization encoding residual, not a general sensitive-name matcher failure. Document and defer; do not add a decoder cascade. The bounded loop is closed with this limitation recorded.

### Last-check results

- Required: `PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest tests/test_invariant_proposals.py tests/test_bridge_investigator.py tests/test_research_context_builder.py -q --tb=short` — **135 passed in 2.17s**.
- Renderer spot checks: single-quoted and spaced assignments redacted; escaped-quote form survived. No provider or live runtime was called; `git diff --check` passed.
