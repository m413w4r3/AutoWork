# Source evidence for PDF-wrapped IOC values

## Rule

The source evidence gate and repair replacement verifier first keep their
existing type-aware, case-sensitive or case-insensitive literal checks. If
those checks fail, an IOC may be checked against a layout-canonicalized view of
the same archived document's parsed text (including PDF text). Sources with a
structured HTML projection keep their stricter existing check so inline markup
cannot make separate HTML tokens appear joined:

- whitespace, NUL, soft hyphen, and zero-width layout characters inside a
candidate are removed from both the value and the source;
- a hyphen immediately before a line break can also be removed as a PDF line
hyphenation artifact;
- blank paragraphs and form-feed page boundaries split the source into
  independent regions;
- every other character remains significant, and the existing IOC-type token
  boundaries remain anchored at both ends;
- a final sentence period may delimit a filename or filepath, while a
  following alphanumeric extension still makes the candidate a longer token;
- the check reads only the parsed text belonging to the IOC's own source
document. Visual-only spans do not establish proof.

`filepath` and `filename` IOC values preserve the existing case-sensitive
comparison. A literal `*` is accepted only for those two artifact types, where
it represents a hunt pattern; it is not expanded to match arbitrary source
text. No wildcard is accepted for domains, hashes, URLs, or other IOC types.

When layout canonicalization is needed, the source gate records
`artifact_verification_whitespace_canonicalized` in extraction diagnostics and
the repair verification response reports
`verification_method=whitespace_canonicalized`. A direct comparison reports
`verification_method=literal` in the repair diagnostic.

## Checkpoint identity

The change bumps `ARTIFACT_VERIFIER_VERSION` to `8`,
`SOURCE_EVIDENCE_VERSION` to `12`, and `SOURCE_TEXT_CONTRACT_VERSION` to
`archived-text-v3`. Future extraction and source-checkpoint identities therefore
change and cannot reuse checkpoints produced under the prior verification
contract. Existing artifacts and checkpoints are not rewritten or recalculated
by this change; their stored status and contents remain unchanged until an
operator explicitly requests new extraction work.

This only changes source verification. It does not alter publication QA,
relevance thresholds, or run-specific behavior.
