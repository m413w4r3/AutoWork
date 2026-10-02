# L3b-1 — versioned subject relevance projection

## Data model and storage

Extraction remains canonical, source-centred, and independent of the subject.
After EXTRACTION, a pure domain projection classifies each fact, event,
uncertainty, indicator/artifact, and rule as `DIRECT`, `CORROBORATION`,
`CONTEXT`, `COUNTER_INDICATION`, `OUT_OF_SCOPE`, or `INDETERMINATE`. Each
decision carries a typed reason code, its target extraction evidence reference,
supporting extraction references, and typed provenance. The projection records
the frozen subject input hash, canonical extraction hash, policy version, and
one decision per extraction item. Construction rejects missing, duplicate, or
unknown references. Indeterminate items stay in the artifact for review and
are excluded from publication inputs.

The deterministic projection is a separate `ProductionArtifact` stage between
EXTRACTION and SYNTHESIS. Its strict canonical JSON body is stored in the
existing artifact blob store; the row links it to the production run and
subject, records its version and functional input hash, and stores bounded
policy/hash metadata. This follows the SYNTHESIS and EDITORIAL_ENRICHMENT
artifact lifecycle and keeps PostgreSQL limited to metadata and blob hashes.
An artifact-only stage would avoid a `ProductionStage`, but the current pipeline
requires a one-to-one mapping between artifact stages and ordered production
stages. Retry, resume, status, and prerequisite checks all use that order, so
adding `RELEVANCE_PROJECTION` as a typed pipeline stage is the least-invasive
way to make synthesis gating explicit while reusing the existing storage path.
The projection hash is SHA-256 over canonical versioned output, including its
subject and extraction identities. Its input hash covers the frozen subject
input, extraction hash, classifier/policy versions, and classifier input
policy. No extraction checkpoint or source identity changes.

## Classification and consumption

The application exposes a classifier protocol. L3b-1 wires only its
deterministic implementation: exact subject actor/campaign terms in an item
and its local context; source tier and editorial role; and exact co-occurring
extraction evidence. A primary CORE item retains the existing DIRECT treatment
for narrative evidence unless an explicit other-actor or counter-indication
rule overrides it; an exact frozen subject anchor adds a specific relation
reason. A matching independent source can corroborate; declared
counter-analysis with an explicit denial is a counter-indication.
Complementary sources without a demonstrated relation are CONTEXT or
INDETERMINATE. Items without a safe relation remain INDETERMINATE; clearly
unrelated actor/event markers are OUT_OF_SCOPE. IOC publication also
requires a demonstrated malicious role: generic filenames and footer contacts
are retained with exclusion reasons, and an IOC from another actor is not
attributed to this subject based only on a shared geopolitical link.

Synthesis receives only projected facts/events and uncertainties not marked
OUT_OF_SCOPE or INDETERMINATE. Dates retain their original `date_text`; a
resolved date is only a sort key. Equivalent timeline and uncertainty wording
is deduplicated. Uncertainties are ranked by a documented deterministic impact
order (attribution, causality, scope, chronology, then other analytic gaps)
before the existing cap. Assembly publishes confirmed IOCs only when the
projection is DIRECT or CORROBORATION with a malicious subject relation. Other
ambiguous material remains reviewable in the projection artifact.

Projection identity/hash is an input to synthesis and enrichment hashes, and
their evidence-pack policy versions advance. Any subject, extraction,
classification-policy, or output change therefore invalidates downstream
reuse. Replaying the stage over the same frozen inputs yields identical JSON;
two subjects sharing one capture reuse its extraction but receive distinct
projection identities.

## L3b-2 extension and limits

L3b-2 can implement the classifier protocol with model-proposed decisions.
The domain validator will still require exact extraction refs and preserve
decision provenance; model proposals do not bypass lineage checks. Human
reviews can later use the same typed decision shape. The default policy is
intentionally conservative but lexical: it cannot reliably resolve aliases,
implicit multi-actor mentions, or whether two technical observations are the
same campaign. Precise multi-actor filtering and subtle Chainalysis/Bitquery
relationships require the L3b-2 classifier and review; no model call occurs in
L3b-1.
