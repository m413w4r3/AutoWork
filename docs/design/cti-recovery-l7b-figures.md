# L7b figure selection and resource proposals

L7b adds a text catalog to Editorial Enrichment for media already collected by
L7a. Stable request-local handles (`F001`, `F002`, …) are assigned only to
items from FULL sources. Catalog rows include the source role, available
caption/alt/figcaption/heading context, page and anchor, dimensions, original
asset location, a provenance summary, and the deterministic inventory
decision. Source document identities are kept out of the model prompt.

The current `ModelRequest` contract carries text, metadata, parameters, and
conversation state; it has no image or thumbnail input. Binary values are
rejected from request metadata. The active ChatGPT bridge route therefore
receives textual figure context only. Visual thumbnails remain a gap until the
gateway and route define a supported attachment contract.

The model may propose `FIGURE` blocks that refer to an accepted handle, cite
evidence handles from that same source, copy a caption grounded in the archived
caption/alt/heading or cited source evidence, choose an existing placement, and
give a reason. Unsupported captions are downgraded to the archived source
caption and recorded as a warning. Unknown handles, deterministic exclusions,
unarchived candidates, missing placements, unknown placements, and
cross-source evidence are rejected with reason codes. Accepted selections use
the archived blob hash, MIME type, dimensions, source provenance, and local
media projection; a zero-figure result is valid. Figure decision traces record
the actor, decision, reason, and prompt/contract/parser/policy versions.

PublicationDocument V4 and V5 already retain figure source URL, original asset
URL, source identity, locator, and provenance in review JSON. The Typst figure
projection uses a local staged media path and does not include the original
asset URL, so a figure never fetches a remote URL during render.

An optional `NEEDS` block records a typed `MEDIA` or `TECHNICAL_ANALYSIS` need,
reason, and bounded query hint in the canonical enrichment review artifact and
stage diagnostics. A second call may propose candidate URLs only when
`PRODUCTION_EDITORIAL_RESOURCE_SEARCH_ENABLED=true`, at least one valid need
exists, and the source access policy allows external submission. The flag is
false by default. Candidate URLs and justifications remain review proposals;
they are not collected, made canonical, or published automatically. Any facts
from a candidate must later pass REFERENCES, archived collection, EXTRACTION,
and subject relevance projection. Model conversations and answers never become
canonical sources.

Enrichment prompt, proposal contract, parser, artifact schema, generator, and
validator versions were advanced for this contract. Parsing remains bound to
verified raw output, parser/contract/prompt versions, evidence handle mapping,
and figure handle mapping; parser-only changes can reuse compatible archived
responses, while prompt changes require a new invocation.
