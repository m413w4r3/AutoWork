# Typst PDF composition QA — opinion v8

**Verdict: FAIL for layout approval.** The PDF rendered and is readable in its main narrative, but the literal table markup and dense IOC appendix make this draft unsuitable for editorial signoff as laid out.

## Identity and checks

- Draft PDF: `.autowork-runs/m07/editorial/opinion-v8-draft.pdf`; SHA-256 `b999bc38561d33e0369ca8e2c46bb98da8f2a20f68db496317349fa8df96f300`.
- Opinion source used for text comparison: `/tmp/opinion_v8.json`; SHA-256 `6a88efad0fd0abfa7dcf09c6d6ed9f32d4a5af1ca3030c5b641230b24868247c`.
- PDF metadata: 18 A4 pages, Typst 0.15.1. The renderer uses 2.2 cm side margins and 2.3 cm top/bottom margins. Every page was rasterized at 80 dpi to `pages-v8/page-01.png` through `page-18.png` and visually reviewed.
- Embedded fonts: Hanken Grotesk and Cascadia Mono (regular/italic variants); all listed fonts are embedded. The expected CHAP logo asset is in the template bundle, but it is not visible in this PDF. The generated relationship diagram and caption on p. 6 are present.
- Reference pages rendered and reviewed: CN2609 cover p. 1, summary p. 4, article p. 9, IOC table p. 119; renders are in `pages-v8/ref-*.png`.
- Compared all seven opinion blocks from the supplied JSON with extracted PDF text: 98.0–100% normalized token coverage per block. No raw `[F:`/`[O:` tokens, `[UNTRUSTED …]` placeholder, or mojibake found. Compact `[F1]`-style refs map to the traceability appendix on p. 17. SHA-256 entries on p. 15 are complete and remain on one line. The base artifact UUID on p. 18 wraps at a UUID group separator but remains complete.
- The red “BROUILLON NON APPROUVÉ” footer and page number are visible on all 18 pages. There is no document-level TLP/classification banner; repeated IOC-row text “TLP non précisé” is not a classification banner.
- `pdfinfo` emitted `Syntax Error: Suspects object is wrong type (boolean)` for both the draft and reference PDFs. Page metadata, font listing, text extraction, and rasterization otherwise completed.

## Ranked findings

1. **Pp. 9–10 — Markdown table prints as literal text (content/render-data contract).** The additional-samples block shows pipe delimiters, backticks, and a Markdown separator instead of a table; it is hard to scan and breaks the professional flow. `chpTypst/RENDERER/publication_helpers.typ:30` renders the fallback as ordinary text. Minimal fix: encode the inventory as a structured table/card block, or reformat this opinion text as readable prose before rendering; the current fallback does not parse Markdown.

2. **Pp. 14–17 — exhaustive IOC appendix is too dense in portrait (renderer).** Four columns squeeze status/context into very small text (about 6.5–7.5 pt); paths wrap heavily. Hashes are intact, but the table is difficult to read at print size. CN2609 p. 119 uses a landscape page for its wide IOC table. Minimal fix: render only these appendix pages in landscape with repeated headers, or use full-width indicator cards and less repeated context. Likely owner: `chpTypst/RENDERER/publication_helpers.typ:356–420` plus page style in `chpTypst/UTILS/document_style.typ`.

3. **P. 18 — provenance is forced onto a nearly empty page (renderer).** The page contains only the publication identity line; p. 17 has substantial unused space after the traceability section, and p. 13 is also mostly empty. `chpTypst/RENDERER/publication_helpers.typ:1215–1218` forces a page break before provenance. Minimal fix: remove that break or keep provenance with the preceding appendix while preserving the draft footer.

4. **Pp. 1–2 — opening hierarchy is weaker than the reference (input ordering/presentation).** The title is followed by references and chronology on p. 1; the synthesis starts on p. 2 as a dense text block. CN2609 p. 1 provides a branded opening/index, while p. 4 presents a shaded overview before the article body. Minimal fix: place the synthesis/lead before references and chronology, and give it a distinct summary treatment. Confirm section order in render data before changing the template.

5. **No visible overall classification label (projection/template, conditional on canonical metadata).** No TLP banner appears on the cover or running header. The render data assembled in `backend/src/cti_app/application/typst_rendering.py:839–846` has no TLP field. Minimal fix: if the canonical publication has an authoritative TLP value, project it and display it once in the document header/cover; do not infer it from IOC-row labels.

6. **No CHAP branding in the running header (renderer).** `chpTypst/RENDERER/publication.typ:7–16` replaces the configured report header with a text-only header; the cover and subsequent pages therefore lack the logo visible in CN2609. Minimal fix: compose the document header with the existing `report-header`/logo instead of replacing it.

7. **Pp. 8–9 — source notes are repetitive and visually weak (rendering).** Full URLs recur as “Sources” lines in small gray type rather than compact numbered links. Minimal fix: show linked numbered refs beside each claim and consolidate URL display in a reference list. Likely owner: `chpTypst/RENDERER/publication_helpers.typ:899–910`.

## Overall comparison

The draft keeps the reference family’s Hanken typography, muted teal headings, A4 narrative pages, and readable code font. Its red draft footer is clear and consistent. The main gaps are the missing brand/header treatment and opening summary card, raw Markdown in the opinion, and portrait IOC pages where CN2609 switches to landscape. Figure captioning and provenance references exist; the rendered figure is visible, and the compact evidence references resolve to a traceability appendix.
