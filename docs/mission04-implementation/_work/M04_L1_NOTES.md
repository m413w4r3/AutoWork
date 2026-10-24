# Mission 04 / L1 — escaped JSON credential redaction

**Status:** L1 code and offline validation GREEN. P10 prompt version remains
**NEEDS_DECISION_P10_VERSION**, recorded but not blocking L1 per the
Mission 03 Review and Handoff §2.

**Base:** e120a36f1afa08261fdf0276e0200872973ad8f2
**Worktree:** /home/nill/perso/work/AutoWork_m04_l1
**Scope:** P10 free-text redaction only; no provider, Bridge, MinIO, database,
or CTI access.

## Change

backend/src/cti_app/application/invariant_proposals.py:2301-2304 now lets the
existing _TEXT_ASSIGNMENT matcher recognize an optional backslash before the
existing quote delimiters around a label and value, including escaped double
and single quotes. The existing
_sensitive_name check still decides whether a matched label is sensitive;
the sensitive-label set and its semantics were not changed. No JSON decoding,
recursive parsing, or broader redaction was added.

backend/tests/test_invariant_proposals.py:1124-1160 exercises the real
_normalize_context → _render_p10_prompt route. It checks that the escaped
double- and single-quoted _auth_ values are absent while the neighboring CVE,
documented CTI marker naming
_api_key_/_auth_, and escaped innocent _author_ assignment are byte-for-byte
present. It also checks a signed URL: credential/signature query values are
redacted and the benign _response-content-type_ parameter remains.

No changes were made to _external_llm_allowed_, DNT, RED, source/sample
restriction, provenance, hashes, or prompt size behavior. P10 still has no
prompt-byte ceiling.

## RED evidence

The new test was run before the matcher change with Python 3.12 and Conda
environment variables removed. Command (working directory backend):

    env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/autowork-m04-l1-uv-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin /home/nill/.local/bin/uv run --python 3.12 pytest tests/test_invariant_proposals.py::test_render_p10_prompt_redacts_escaped_json_credential_and_preserves_cti_evidence -q --tb=short

Failure output excerpt:

    F                                                                        [100%]
    E AssertionError: ... Report claims:
    E - claim id=claim-escaped; text=CVE-2025-4242 CTI_EVIDENCE_MARKER: documented api_key/auth fields in malware config {\"auth\": \"FIXTURE_ESCAPED_AUTH\"} author=Jane https://example.invalid/download?X-Amz-Credential=[REDACTED]&X-Amz-Signature=[REDACTED]&response-content-type=application%2Fzip
    E assert 'FIXTURE_ESCAPED_AUTH' not in 'You are preparing ...'
    1 failed in 0.64s

This proves the value survived normalization and appeared in the rendered P10
prompt before the fix. The signed URL was already scrubbed by the existing URL
matcher in this same run.

## GREEN evidence

Commands used uv run --python 3.12, with Conda variables and VIRTUAL_ENV
unset and UV_CACHE_DIR=/tmp/autowork-m04-l1-uv-cache:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/autowork-m04-l1-uv-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short
    59 passed in 0.58s

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/autowork-m04-l1-uv-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --python 3.12 ruff check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py
    All checks passed!

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/autowork-m04-l1-uv-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py
    2 files already formatted

The focused new test also passed (1 passed in 0.33s) after the matcher
change. The full test_invariant_proposals.py file was rerun after the final
test edit and passed as recorded above.

## P10 prompt version decision

P10_PROMPT_VERSION remains "4" at
backend/src/cti_app/application/invariant_proposals.py:67; it was not changed
silently. The escaped credential redaction changes rendered prompt text.
make_proposal_turn_idempotency_key hashes the investigation ID, cycle,
snapshot immutable references, and prompt version
(invariant_proposals.py:921-934); it does not hash the normalized context or
rendered prompt. Consequently, for a snapshot with the same immutable
references, keeping version 4 leaves the key unchanged while bumping to 5
changes it. L1 explicitly permits either choice and the review handoff records
it as a human decision; no active contract resolves the choice safely.

Record: **NEEDS_DECISION_P10_VERSION**. This blocks only settling the prompt
version decision. The matcher and offline validation are complete. The next
permissible action is an explicit owner decision to retain version 4 or bump to
version 5, followed by updating the version assertion/key expectations if
needed.

## Risks and preserved cases

- The generic _auth_, _api_key_, and other existing sensitive labels retain
  their prior behavior. This change recognizes their existing assignment
  syntax when JSON quotes are backslash-escaped.
- The _author_ counterexample, the CTI marker, the CVE, and the benign signed
  URL parameter remain intact in the rendered prompt.
- An unchanged prompt version can reuse the same idempotency key for identical
  immutable references despite changed rendering. The consequences depend on
  prior persisted turns; resolve this through the explicit version decision.
- This bounded matcher change does not decode multiple serialization layers
  or cover arbitrary nonstandard encodings.

## Independent-review correction — repair attempt 1

The independent review at lot 895a24fdf0320257374bc899d81ad91fbc782ab5 found
that the first matcher truncated escaped scalar values at spaces/quotes and
consumed the slash on the escaped closing quote. Attempt 1 adds a dedicated
single-layer escaped-assignment branch before the legacy assignment matcher.
It scans through whitespace and internal escaped quotes until an escaped
closing delimiter is followed by an object/array terminator, end of input, or
the next escaped object key. The replacement emits the captured opener and
closer around REDACTED. The legacy matcher remains in place for ordinary
assignments, and sensitive labels were not changed.

The renderer-path tests now cover escaped values with spaces, one-backslash
and triple-backslash internal quote sequences, and a simple escaped scalar.
Each expects the complete value to disappear and the exact redacted auth value
and adjacent CTI object member to remain. A companion test preserves the
innocent escaped author value with a space, adjacent CTI/CVE markers, and
continues to redact a plain auth assignment. Existing signed-URL coverage
continues to check that benign URL parameters survive.

### RED before attempt 1 matcher change

Command, with Conda and virtual-environment variables removed and Python 3.12:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py::test_render_p10_prompt_redacts_complete_escaped_json_scalar -q --tb=short

    FFF                                                                      [100%]
    SPACE_SUFFIX remained after [REDACTED] for the spaced value.
    TAIL_SUFFIX remained after the internal escaped quotes.
    The simple value was redacted, but the escaped closing delimiter was malformed.
    3 failed in 0.45s

This initial RED run had three parameter cases. After the matcher edit, a
one-backslash internal-quote case was added; it is included in the focused
GREEN run.

### GREEN after attempt 1

Focused renderer tests:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py::test_render_p10_prompt_redacts_complete_escaped_json_scalar tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_escaped_innocent_author_and_plain_assignment -q --tb=short
    5 passed in 0.41s

Full P10 test file:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short
    64 passed in 0.63s

Lint and formatting:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 ruff check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py
    All checks passed!

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py
    2 files already formatted


## Independent-review correction — repair attempt 2

R2 at 895a24fdf0320257374bc899d81ad91fbc782ab5 found three defects in
attempt 1: a [REDACTED] prefix collision leaked a tail, repeated unclosed
assignments caused quadratic regex rescans, and a closing escaped quote was
corrupted when the JSON scalar ended in a backslash. Attempt 2 replaces only
the escaped-assignment matcher with a monotone scanner. It parses each
assignment header once, walks each sensitive scalar once, counts each
backslash run, and preserves the final slash-plus-quote delimiter. The scalar
is treated as already redacted only when its entire content equals exactly
[REDACTED]. The sensitive-name set, _sensitive_name, and the ordinary
assignment matcher are unchanged.

The scanner accepts the existing one-layer syntax: a delimiter followed by
whitespace and an object/array terminator or end of text, or a comma followed
by an escaped quoted key and colon. It does not decode or recursively parse
serialized values. An ambiguous malformed comma/key tail fails closed by
redacting the sensitive value through end of text; this avoids restarting a
suffix scan and leaking an unclosed secret.

### RED before attempt 2 scanner change

Python 3.12 command, from backend, with Conda/venv variables removed and
pytest cache/bytecode disabled:

    env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py::test_render_p10_prompt_redacts_marker_prefix_collision_only_when_not_exact tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_closing_delimiter_after_trailing_backslash tests/test_invariant_proposals.py::test_render_p10_prompt_scans_many_unclosed_escaped_assignments_linearly -q --tb=short

Observed before the scanner fix:

    FF...F
    marker-collision test: FIXTURE_MARKER_TAIL appeared after [REDACTED].
    trailing-backslash test: the closing delimiter lost its backslash.
    unclosed-input test: FIXTURE_UNCLOSED_TAIL_SECRET appeared in the rendered prompt.
    3 failed in 3.48s

The R2 reviewer measured the old matcher at 751 ms for 12,813 characters /
800 labels and 2,969 ms for 25,613 characters / 1,600 labels. The combined RED
command above also took 3.48 seconds. Its performance test checks secret
absence before its timing assertion, so the original run establishes RED by
leak and command wall time; the corrected test below times normalization
through rendering.

### GREEN after attempt 2

Focused renderer regressions, including all four scalar parameters, the
innocent author and plain assignment cases, marker collision, trailing
backslash, and unclosed-input performance guard:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py::test_render_p10_prompt_redacts_complete_escaped_json_scalar tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_escaped_innocent_author_and_plain_assignment tests/test_invariant_proposals.py::test_render_p10_prompt_redacts_marker_prefix_collision_only_when_not_exact tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_closing_delimiter_after_trailing_backslash tests/test_invariant_proposals.py::test_render_p10_prompt_scans_many_unclosed_escaped_assignments_linearly -q --tb=short
    8 passed in 0.40s

The standalone measured _normalize_context -> _render_p10_prompt seam on the
same 25,613-character / 1,600-label input took 0.003429 seconds; the tail
secret was absent. The test itself has a generous 1.0-second guard and is an
anti-DoS regression check, not a prompt-size limit. Its dedicated pytest run
passed: 1 passed in 0.39s.

Full P10 file, after the final test edit:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short
    67 passed in 0.64s

Ruff checks, Python 3.12:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 ruff check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py
    All checks passed!

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV UV_CACHE_DIR=/tmp/m04-l1-uv-noproject-cache PATH=/home/nill/.local/bin:/usr/local/bin:/usr/bin:/bin uv run --no-project --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py
    2 files already formatted

The author value Jane Doe, adjacent CVE/CTI evidence, CTI object member,
and benign signed-URL response-content-type remain byte-for-byte. Existing
plain auth=FIXTURE_PLAIN_AUTH coverage remains active. Signed credential
and signature URL parameters remain redacted by their existing URL matcher.
No label was added, and no field named auth is redacted unless it is in the
existing sensitive-assignment syntax.

### P10 version and residual risk

Attempt 2 changes rendered text for escaped sensitive assignments. The
idempotency key still hashes the investigation/cycle, immutable snapshot
references, and P10_PROMPT_VERSION; it does not hash the normalized text or
rendered prompt. With the version unchanged, the key therefore does not
change for the same immutable references even though this rendering does.
P10_PROMPT_VERSION remains 4; no silent bump was made. The recorded
NEEDS_DECISION_P10_VERSION remains an owner decision under Review and
Handoff §2 and is independent of this scanner repair.

Residual risk: support remains intentionally limited to one escaped layer.
For an ambiguous malformed sensitive value followed by comma/key-like text,
the fail-closed path may redact the remainder of that claim and remove
adjacent evidence. Canonical adjacent fields and all required innocent
counterexamples are covered by the renderer-path tests. No prompt-byte ceiling
was introduced.

## Independent-review correction — repair attempt 3

Attempt 3 starts from reviewed HEAD
0f5371e06b22a35c17d4d4677be124967a601186. It supersedes attempt 2's
fail-closed-to-EOF behavior where a structural object terminator can be
identified. The scanner indexes escaped quote candidates, structural quotes,
and object/array terminators in a single pass, then advances monotone cursors
while matching sensitive assignments. It does not rescan suffixes.

For one escaped JSON layer, a structural delimiter quote is preceded by a
backslash run congruent to 1 modulo 4. An escaped quote inside the scalar is
preceded by a run congruent to 3 modulo 4. Thus runs 1, 5, ... preserve the
closing delimiter, while runs 3, 7, ... remain inside the scalar. Runs 3 or 7
immediately before an embedded object/array terminator do not cause premature
closure. A malformed next key after a structural close quote is irrelevant:
comma, object/array terminator, or end of text is sufficient to close the
scalar and preserve the malformed tail. When an unclosed value reaches an
object/array terminator, or a comma followed by a structural escaped key,
before a later close quote, fallback redacts only to that separator and
preserves the following CTI text.

The marker idempotence check still requires the entire escaped scalar to equal
[REDACTED]. The code changes only the escaped-assignment scanner and its
tests. _SENSITIVE_NAMES, _sensitive_name, and the ordinary assignment matcher
are unchanged; no recursive JSON decoding was added.

### RED before attempt 3 scanner correction

Working directory was backend, Python 3.12. The renderer-path RED command
was:

    uv run --python 3.12 pytest tests/test_invariant_proposals.py::test_render_p10_prompt_keeps_internal_escaped_quote_inside_auth_scalar tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_evidence_after_malformed_adjacent_key tests/test_invariant_proposals.py::test_render_p10_prompt_bounds_unclosed_value_at_object_terminator tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_delimiters_for_trailing_backslash_counts -q --tb=short

Observed before the attempt 3 scanner correction:

    FFFFFF........                                                         [100%]
    6 failed, 8 passed in 0.49s

All four object/array and backslash-prefix variants leaked
CTI_AFTER_INNER_QUOTE; the malformed adjacent-key case lost its CVE and
observation tail; and the unclosed-object case lost the text after the
terminator. The eight trailing-backslash-count cases passed on the previous
scanner. The separate counterexample supplied by the independent review was
also added: an unclosed auth followed by an object terminator, CVE, and
escaped evidence key must not associate the later evidence-value close quote
with auth.

### GREEN after attempt 3

Focused renderer-path group, including the reviewer counterexample,
marker-prefix collision, unclosed-input DoS guard, innocent author, plain
auth, and all trailing-backslash cases:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV uv run --python 3.12 pytest tests/test_invariant_proposals.py::test_render_p10_prompt_keeps_internal_escaped_quote_inside_auth_scalar tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_evidence_after_malformed_adjacent_key tests/test_invariant_proposals.py::test_render_p10_prompt_bounds_unclosed_value_at_object_terminator tests/test_invariant_proposals.py::test_render_p10_prompt_bounds_unclosed_auth_before_later_evidence_quote tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_delimiters_for_trailing_backslash_counts tests/test_invariant_proposals.py::test_render_p10_prompt_redacts_marker_prefix_collision_only_when_not_exact tests/test_invariant_proposals.py::test_render_p10_prompt_scans_many_unclosed_escaped_assignments_linearly tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_escaped_innocent_author_and_plain_assignment tests/test_invariant_proposals.py::test_render_p10_prompt_preserves_closing_delimiter_after_trailing_backslash -q --tb=short --durations=8
    20 passed in 0.36s

Full P10 test file:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV uv run --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short --durations=10
    83 passed in 0.60s

The same 25,613-character / 1,600-label unclosed-input fixture measured
normalization through rendering at 0.004307 seconds; the tail secret was
absent. The existing renderer test retains a generous 1.0-second guard. This
is an anti-DoS regression guard, not a prompt-size ceiling. The full P10 suite
also passed its 3,145,745-byte no-ceiling test.

Ruff, Python 3.12:

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV uv run --python 3.12 ruff check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py
    All checks passed!

    cd backend && env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER -u _CE_CONDA -u _CE_M -u VIRTUAL_ENV uv run --python 3.12 ruff format --check src/cti_app/application/invariant_proposals.py tests/test_invariant_proposals.py
    2 files already formatted

The renderer tests preserve the neighboring CVE, the documented
api_key/auth evidence marker, CTI_EVIDENCE, malformed-tail observation, and
escaped author: Jane Doe byte-for-byte. The benign signed-URL
response-content-type parameter remains unchanged; existing URL credential
and signature redaction remains active. The plain auth=FIXTURE_PLAIN_AUTH
assignment is still redacted.

### P10 version decision and residual risk

The output rendering changes, but the idempotency key does not incorporate
the normalized text or rendered prompt; with the same immutable snapshot
references and version it remains unchanged. P10_PROMPT_VERSION remains 4.
No contract in Review and Handoff §2 resolves whether that rendering change
should bump the prompt version, so NEEDS_DECISION_P10_VERSION remains for the
owning decision maker; it was not changed silently.

Residual risk: escaped assignment recognition is deliberately limited to one
serialization layer and the existing sensitive labels. Malformed input with
no recognizable object/array terminator still fails closed through the
remainder of the claim. The scanner uses linear auxiliary indexes
proportional to the claim text; no proof or prompt-size ceiling was added.
