# M02-S1 permissive evidence review

- **TASK_ID:** M02-S1-REVIEW
- **Compared:** `e4abba495d9f5fdc296e2ba80b65442855625474..892e5f45518b350506db85931a629bcbd5216430`
- **First-pass verdict at `892e5f45`: REJECT** — the nested secret preflight bypass was a disclosure boundary failure; explicit evidence could also be omitted.
- **Scope:** source review and synthetic offline probes through `execute` with in-process fake transport; no live provider calls.

## Findings (ranked)

1. **SUPERSEDED by policy clarification — VERIFIED behavior, not a defect:** a nested generic `api_key: {base64: ...}` value reached fake transport. The project policy permits generic CTI `api_key` evidence; only AutoWork operational credentials are blocked. This first-pass finding is withdrawn as a code defect. No real credential was used. See `backend/src/cti_app/application/advisor_dossier.py:255-267` and `backend/src/cti_app/application/investigation_advisor_tools.py:358-369`.

2. **HIGH — VERIFIED / INFERENCE: sample-level restrictions are not checked on the measurement path.** `_sample_blob_for_hash` verifies association, state, blob hash and quarantine bucket, but does not inspect sample `do_not_submit`, `external_llm_allowed` or TLP fields; qualifying measurements are then added to prompt data. A synthetic `execute` probe with case-level authorization allowed and sample flags restrictive still called fake transport with the long hex/Base64 measurements. **INFERENCE:** if those sample flags independently restrict disclosure, this path sends restricted sample-derived content. Upstream folding into the case envelope is **UNKNOWN**. See `backend/src/cti_app/application/investigation_advisor_tools.py:1817-1853,2711-2739`.

3. **HIGH — VERIFIED: explicitly referenced observations can lose permitted content.** Relevance selection keeps only excerpts scoring above zero. If any excerpt matches, the explicit-reference fallback does not run; it only adds the single best excerpt when none match. A synthetic observation with relevant DNS text and separate 600-byte evidence lost the latter from the dossier. See `backend/src/cti_app/application/advisor_dossier.py:477-510`.

4. **MEDIUM — VERIFIED: nested `bytes[].raw` content is dropped.** The `bytes` special route sends rows to `add_short_hex_rows`, which accepts `value`, `bytes`, `hex` or `content`, but not `raw`; a direct extraction probe of `{"bytes":[{"offset":4,"raw":"00ff"}]}` yielded no excerpt. See `backend/src/cti_app/application/advisor_dossier.py:158-186,233-248`.

5. **LOW — VERIFIED: exact text dedupe can erase distinct field labels.** Ordinary excerpts dedupe on `(kind, text)`, excluding title/path, so equal text under different fields collapses to the first title. Content bytes remain, but field context is lost. See `backend/src/cti_app/application/advisor_dossier.py:130-149`.

## Axis A — completeness and provenance

- **VERIFIED:** the 512-byte, 16,384-byte and Base64/hexdump format refusals are removed; no equivalent format-specific refusal remains. New `execute`/fake-transport tests cover DNS raw 600-byte Base64, spaced/fragmented Base64, two independent 300-byte values, large hex and Base64 measurements, >18,000-byte text and large hex, and contradictory DNS evidence. Long text is passed intact; it is no longer sentence-split. See `backend/tests/test_investigation_advisor_tools.py:1810-2018`.
- **VERIFIED:** retained dossier rows still carry evidence IDs, source SHA-256, body SHA-256/byte length, rendered SHA-256/byte length, aliases and source extras; offsets remain on measurement/excerpt rows. See `backend/src/cti_app/application/advisor_dossier.py:903-924` and `backend/src/cti_app/application/advisor_wire.py:90-260`.
- **VERIFIED:** the unchanged `_OMIT_KEYS` list and local-command output skip remain possible omission sites. The latter has a verified measurement route; **UNKNOWN** whether canonical producers place permitted evidence under omitted metadata keys. See `backend/src/cti_app/application/advisor_dossier.py:70-94,203-248`.

## Axis B — disclosure controls and audit

- **VERIFIED:** case snapshot/envelope authorization still runs before consultation data, redaction, dossier construction or transport and returns `POLICY_DENIED`. The denial test asserts zero fake transport calls; the source gate includes `external_llm_allowed`, `do_not_submit` and TLP RED checks. The new permissive paths do not bypass this case-level gate. See `backend/src/cti_app/application/investigation_advisor_tools.py:323-337`, `backend/src/cti_app/application/investigation_service.py:2555-2570`, and `backend/tests/test_investigation_advisor_tools.py:3162-3191`.
- **VERIFIED:** removing `content_omission_codes` did not remove missing-evidence records: prompt-size/evidence-preflight failures still carry `missing_refs`, reasons and completeness metadata. See `backend/src/cti_app/application/investigation_advisor_tools.py:416-455` and `backend/src/cti_app/application/advisor_dossier.py:1537-1547`.

## Regressions, simplicity and checks

- **VERIFIED:** removed refusal symbols have no remaining backend importers/callers; `advisor_wire.py` retains the dossier aliases and provenance mapping. The change deletes the custom Base64 decoder and fixed-size checks, while extending the existing content-key set and adding a path classifier; it adds no separate decoder or new module layer.
- **VERIFIED:** requested pytest command: **135 passed, 1 failed**. The sole failure was `test_archived_corpus_c01_to_c13_replays_as_compact_resolvable_text_dossiers`: its `.autowork-runs/run12-editorial-2026-10-20` fixture is gitignored and absent from this worktree, as expected; this is not a code failure.
- **VERIFIED:** `ruff check` on the five changed backend files passed. `ruff format --check` on those files passed (already formatted).
- **VERIFIED:** the two additional fake-transport `execute` probes reproduced the secret-field and sample-flag findings; a direct extractor probe reproduced `bytes[].raw` and mixed-relevance omissions. All used synthetic data and no real transport.

Commands run:

```sh
# Run from backend/
PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest tests/test_advisor_completeness.py tests/test_investigation_advisor_tools.py tests/test_advisor_wire.py tests/test_advisor_anti_slop_eval.py -q --tb=short
/home/nill/perso/work/AutoWork/backend/.venv/bin/ruff check src/cti_app/application/advisor_dossier.py src/cti_app/application/investigation_advisor_tools.py tests/test_advisor_completeness.py tests/test_advisor_wire.py tests/test_investigation_advisor_tools.py
/home/nill/perso/work/AutoWork/backend/.venv/bin/ruff format --check src/cti_app/application/advisor_dossier.py src/cti_app/application/investigation_advisor_tools.py tests/test_advisor_completeness.py tests/test_advisor_wire.py tests/test_investigation_advisor_tools.py
```

## First-pass next action (superseded)

Repair the secret projection/preflight boundary and sample-level restriction checks, then retain all content for explicitly referenced observations; add regression tests for these paths before reconsidering approval.

## Re-review after 6f40b125

- **Verdict: REVISE.** Review merge HEAD is `bfb9d4447038d687a87cb3cb92830fc725a5d22b` and includes `6f40b125`. The five specific payload-shape fixes are present and the generic `api_key` finding is withdrawn under the clarified policy; a DotNet sample-derived path still bypasses the new restriction gate, and the changed module fails mypy.
- **VERIFIED — closed:** operational fields `vt_api_key`, `operator_token`, `service_password` and `presigned_url` are skipped by the dossier projection; generic `api_key` remains projectable as required by the stated CTI policy. Tests cover operational `vt_api_key` and permitted generic `api_key`; direct synthetic predicate probes covered all four labels. Explicit references keep all excerpts, `bytes[].raw` keeps its offset, and dedupe distinguishes titles. See `backend/src/cti_app/application/advisor_dossier.py:140,202-210,158-186,477-510`, `backend/src/cti_app/application/publication_secret_guard.py:39-45,224-234`, `backend/tests/test_advisor_completeness.py:523-573`, and `backend/tests/test_investigation_advisor_tools.py:2062-2140`.
- **HIGH — VERIFIED path / INFERENCE policy impact:** sample filtering only recognizes `object_type == "sample"` with exact hash equality (`backend/src/cti_app/application/investigation_advisor_tools.py:328-336`). The canonical DotNet producer stores `object_type="dotnet_analysis"` and prefixes `object_id` with the sample SHA (`backend/src/cti_app/application/investigation_dotnet_tools.py:417-425`). A fake-transport `execute` probe using the producer's nested `result.methods` shape, a `do_not_submit=True` sample and an explicit DotNet reference sent its synthetic method marker once; execution succeeded. **INFERENCE:** this violates the sample restriction for analysis-derived sample evidence. Add prefix/type-linked DotNet coverage and block it.
- **HIGH — VERIFIED path / UNKNOWN policy scope:** plan consultations still select relevant latest finding statements and their observation references (`backend/src/cti_app/application/investigation_advisor_tools.py:2066-2082`; `backend/src/cti_app/application/advisor_dossier.py:535-580`) after restricted observations are filtered. A fake-transport probe confirmed a finding linked to a restricted sample observation can reach transport while the observation body is absent. Whether sample restrictions bind downstream finding summaries is **UNKNOWN**; clarify and enforce the intended boundary.
- **LOW — VERIFIED type-check regression:** `seen` is annotated as `set[tuple[str, str]]`, while dedupe now inserts a three-field tuple (`backend/src/cti_app/application/advisor_dossier.py:117,140,144`). `mypy src/cti_app/application/advisor_dossier.py` reports errors at lines 141 and 144. This does not affect runtime behavior, but the configured backend typecheck (`backend/pyproject.toml:95-100`) will fail.
- **VERIFIED:** case-level `external_llm_allowed`, `do_not_submit` and TLP RED denial still occurs before consultation data and transport (`backend/src/cti_app/application/investigation_advisor_tools.py:362-376`); its zero-call test is `backend/tests/test_investigation_advisor_tools.py:3372-3401`. Explicit restricted sample refs return `POLICY_BLOCKED` with zero fake transport calls (`backend/tests/test_investigation_advisor_tools.py:2143-2185`). Operational secret redaction still precedes dossier creation (`backend/src/cti_app/application/investigation_advisor_tools.py:397-408`). No generic `api_key` policy conflict was found.
- **Tests:** the requested pytest command below produced **144 passed, 1 failed**. The sole failure remains the archived-corpus replay because its `.autowork-runs/` fixture is gitignored and absent from this worktree. `ruff check` and `ruff format --check` passed on all five changed files. `mypy src/cti_app/application/advisor_dossier.py` failed with the two tuple-annotation errors above. The two additional disclosure probes used synthetic values and fake transport only.

```sh
# Run from backend/
PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest tests/test_advisor_completeness.py tests/test_investigation_advisor_tools.py tests/test_advisor_wire.py tests/test_advisor_anti_slop_eval.py -q --tb=short
```
