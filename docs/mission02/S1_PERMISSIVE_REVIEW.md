# M02-S1 permissive evidence review

- **TASK_ID:** M02-S1-REVIEW
- **Compared:** `e4abba495d9f5fdc296e2ba80b65442855625474..892e5f45518b350506db85931a629bcbd5216430`
- **Verdict: REJECT** — the nested secret preflight bypass is a disclosure boundary failure; explicit evidence can also be omitted.
- **Scope:** source review and synthetic offline probes through `execute` with in-process fake transport; no live provider calls.

## Findings (ranked)

1. **HIGH — VERIFIED: nested secret-labelled value bypasses secret preflight.** An observation field shaped as `api_key: {base64: ...}` is projected to a plain dossier excerpt by the new content walk, losing the parent secret label before the transport prompt is made. `_redact_advisor_payload` and `_marked_secret_field_survives` run before dossier construction, but the fake-transport `execute` probe confirmed the synthetic marker reached transport. No real credential was used. See `backend/src/cti_app/application/advisor_dossier.py:255-267` and `backend/src/cti_app/application/investigation_advisor_tools.py:358-369`.

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

## Next action

Repair the secret projection/preflight boundary and sample-level restriction checks, then retain all content for explicitly referenced observations; add regression tests for these paths before reconsidering approval.
