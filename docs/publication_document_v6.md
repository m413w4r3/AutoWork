# PublicationDocumentV6

`PublicationDocumentV6` is an additive wrapper around one exact
`PublicationDocumentV5`. Its `base_publication` field preserves the V5
synthesis, extraction projection, references, indicators, and editorial
enrichment byte-for-byte after canonical serialization. V4, V5, and V6 remain
readable through `publication_document_from_json`; normal production assembly
continues to write V5. The augmentation artifact service is the only supported
path that persists V6.

## Public API

- `PublicationDocumentV6` stores `base_publication`, required base provenance,
  and an optional `AnalystOpinionV1` plus its render projection.
- `publication_document_v5_sha256` computes the canonical V5 document hash used
  to bind the wrapper to its embedded base.
- `augment_publication_with_opinion(base_publication_document, opinion,
  citation_context)` validates approval, identities, citations, and disclosure
  flags, then returns an immutable V6 value. It does not persist or mutate its
  inputs.
- `PublicationAugmentationArtifactService.publish(...)` verifies the current
  V5 and related verified investigation/synthesis artifacts, writes a new
  versioned `PUBLICATION` blob, records `previous_publication_artifact_id`, and
  optionally renders its preview. It does not execute a production workflow or
  create a new stage.
- `qa_publication_v6(...)` applies the existing publication projection QA,
  checks the wrapper invariants and conditional section order, and returns
  stable `error_codes` for V6 failures. Existing V5 QA result shape is retained.

The rendered article order is `references`, `synthesis`, optional
`analyst_opinion`, optional `technical_annex`. The opinion uses the heading
“Avis de l'analyste”; its blocks follow `OpinionBlockType` order, show the
fact/inference/hypothesis qualification and confidence, link finding and
evidence UUIDs, and include unresolved points. No opinion means no opinion
section. A V5 document still serializes and renders as V5.

## V6 reader handling after H1b

| Reader | V6 handling |
| --- | --- |
| `domain/publication_document.py` codec | Reads V4/V5/V6; typed base mismatch and opinion invariant errors. |
| `application/publication_rendering.py` and `application/typst_rendering.py` | Render V6 opinion while projecting the embedded V5 body. |
| `application/publication_qa.py` | Checks V6 invariants and conditional section order; V5 contract remains unchanged. |
| `application/subject_content.py` and `api/subject_content.py` | Project V6 to its base V5 for existing Subject JSON and asset consumers; the Subject UI remains on the V5 projection. |
| `api/production.py::_artifact_view_for_run` | Returns HTTP 409 with `publication_document_v6_requires_h1b` to the legacy raw artifact viewer. |
| `application/edition_document.py::build_edition_document` | Carries the exact pinned V6 wrapper, including its V5 base, rendered opinion, and provenance, in a composed multi-batch edition. |
| `application/edition_rule_archive.py::_read_article` | Reads the title from the embedded V5 base. |
| `application/edition_ioc_export.py::project_publication_iocs` and `application/edition_classification.py::classify_edition_publications` | Continue through the edition's base V5 projection. Their existing V5 data is preserved by the edition document builder. |
| `domain/edition_publication.py::EditionDocumentV2.from_json` | Its V2 codec accepts the V4/V5/V6 publication union; the builder stores the pinned wrapper intact. |
| `application/edition_typst_rendering.py::EditionTypstRenderer.render` | Supports V6 through the shared publication projection; the rendered opinion follows the V5 article body. |
| `application/publication_builder.py`, `application/publication_assembly.py`, `application/production_workflow.py`, `application/production_repairs.py` | These are V5 producers/read their own newly assembled V5 output; H1 does not route augmentation through them. |
| `frontend/src/api/production.ts` and `frontend/src/components/ProductionArtifactView.tsx` | Existing wrapper shape is not compatible with V6. The production artifact API rejects V6 before those views receive it; Subject and edition JSON remain V5 projections. |

## UI follow-up outside H1b

H1b implements the backend composed-edition path. UI consumers still need a
separate update to understand and display the V6 wrapper:

- `frontend/src/api/production.ts` (`PublicationDocumentV6`),
  `frontend/src/components/ProductionArtifactView.tsx` (`isPublicationDocument`,
  `PublicationDocumentView`), `frontend/src/api/subjectContent.ts`,
  `frontend/src/features/subject/ArticleTab.tsx`, `frontend/src/api/publication.ts`,
  and `frontend/src/features/edition-workflow/EditionPreviewPanel.tsx` for wrapper
  and composed-edition UI handling.

## Édition composée (H1b)

`EditionDocumentV2` remains the composed-document contract. Its publication
codec already accepts the canonical V4/V5/V6 union, so H1b keeps the schema at
V2 and adds no redundant edition schema version. Each pinned V6 publication is
serialized intact inside its `EditionPublicationV2`: the immutable V5 base,
`rendered_opinion`, and V6 provenance (base artifact/hash and opinion
id/version/hash) remain in the frozen edition document. V5 publications keep
their prior serialized form.

The edition renderer accepts V6 and projects the same ordered sections as the
standalone publication: `references → synthesis → analyst_opinion` when present,
then `technical_annex` when present. A V5 article or V6 without an opinion emits
no opinion section. Classification and IOC export continue to read the
delegated V5 fields; opinion content does not change either result.

Composition still pins the exact PUBLICATION artifact/run/version/hash.
Approving an opinion creates a linked V6 artifact but does not mutate the
composition, manifest, release, or PDF. Only the existing versioned human
composition mutation can repin the article. Historical manifests/releases and
their PDF evidence remain readable. The raw production artifact viewer and
other readers not implemented for V6 keep their explicit typed rejection.

## Validation evidence

`tests/test_typst_chp_parity.py::test_real_typst_and_poppler_render_v6_opinion_without_changing_v5`
compiles a V5 PDF before augmentation, renders V6, then recompiles the same V5.
It compares the old V5 canonical artifact bytes and both V5 PDFs, extracts the
V6 PDF text with Poppler, checks its opinion heading and qualification, verifies
clickable URI annotations, and rasterizes a page with `pdftoppm`. The W11
workflow-emulator test separately checks the pre-H1b V5 edition-document bytes,
rechecks the persisted PDF SHA-256 before and after opinion approval, and proves
that only an explicit versioned composition mutation adds A's opinion.
