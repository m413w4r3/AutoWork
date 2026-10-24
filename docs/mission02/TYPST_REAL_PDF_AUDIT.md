# M02-S6 — Typst real PDF audit

Base: `e4abba495d9f5fdc296e2ba80b65442855625474`

Result scope: read-only inspection plus this audit record; no renderer or opinion changes.

## Status

- `INSPECTED_REAL_PDF_READ_ONLY`: yes.
- `RENDERED_OFFLINE`: yes. The real renderer test compiled the synthetic V4 fixture with Typst 0.15.1; the focused three-file suite also passed.
- `BLOCKED_FONTS`: no. The first attempt was blocked by the worktree's omitted, gitignored font bundle; the locked bundle was then used read-only from the main checkout.
- `EDITORIAL_DECISION_REQUIRED`: yes; identity, reference placement, and the separate provenance page differ from historical bulletin layouts.
- `UNKNOWN`: the supplied PDF's canonical document schema and whether any sections/assets are absent from it; the canonical artifact JSON was not an authorized input.

## Subject PDF

- Input: `/home/nill/.cache/autowork-mission02/blinder-publication-latest-clean.pdf`
- SHA-256: `d4abc34524e0514692e686bc52cd344c0384ce14f77be7109d88e20027ce89e6`; 296,369 bytes; 14 pages; A4; PDF 1.7; Creator `Typst 0.15.1`.
- Provenance supplied with the input: renderer `publication-v14-typst-v32-literal-notes`, template `chp-article-v33-literal-analyst-notes`, policy `typst-publication-v7-chapsvision-cited-context`, font label `chp-fonts-v1`. This is a current-code-draft preview, not a validated final bulletin.
- `pdffonts` shows embedded Hanken Grotesk and Cascadia Mono subsets. Poppler emitted `Suspects object is wrong type (boolean)` while still returning metadata, text, and page renders; the same warning appears on the historical PDFs.
- Visually inspected pages 1, 6, 8–14: opening structure/references, narrative, IOC lists, technical tables, and final provenance. The final two table pages were checked at high resolution. No subject PDF or PNG was added to Git.

## References inventoried

The four read-only trees were under `/home/nill/perso/work/AutoWork/docs/references/`:

| Tree | Files/elements inspected | Version and fonts |
|---|---|---|
| `RU2609_typ` | `TEMPLATES/article.typ`, `breve.typ`, `utils.typ`; `UTILS/colors.typ`, `header_footer.typ`, `helpers.typ`; `main.typ`; `reports/table_reporting.typ`; approved PDF pages 1 and 20. | Source has no Typst version pin. PDF metadata says Typst 0.15.1. 18 Hanken Grotesk TTFs; no Cascadia files. |
| `CN2609_typ` | Same template/helper/main/report paths; approved PDF pages 1 and 34. | Source has no Typst version pin. PDF metadata says Typst 0.15.1. 18 Hanken Grotesk TTFs; no Cascadia files. |
| `chpTypst` | `TEMPLATES/article.typ`, `breve.typ`, `utils.typ`; `UTILS/*`; `main.typ`. | No version pin found in the reference tree. 18 Hanken Grotesk TTFs; no Cascadia files. |
| `typstPipeline` | `ioc_reporter_typst.py`; `pays-YYMM_template/TEMPLATES/article.typ`, `breve.typ`, `utils.typ`; `pays-YYMM_template/UTILS/*`, `main.typ`, `reports/table_reporting.typ`. | No version pin found. 18 Hanken Grotesk TTFs; no Cascadia files. No PDF is present in these two code-only reference trees. |

Historical source uses Hanken Grotesk at 11 pt and Cascadia Mono for raw/code styling (for example RU/CN `main.typ:8–12,47`). The RU/CN PDFs show a bulletin masthead, issue/date identity, contents cards, and per-page footnote links. Their article pages use narrative text, tables, and source links in the footer area. These are full bulletin outputs, unlike this single-publication preview.

`isNew` is not a publication confidence/status field. The RU helper filters `isNew == "0"` into its general IOC group and `"1"` into “IOC originaux” (`RU2609_typ/UTILS/helpers.typ:504–512`). The pipeline parser accepts `isNew`/`is_new` and treats `1`, `true`, `yes`, `y`, and `oui` as true (`typstPipeline/ioc_reporter_typst.py:413–427`); `origin_label` maps that boolean to configurable analyst/source labels (`:1277–1282`). Do not convert this legacy split into current retained/uncertain/excluded dispositions.

## Findings, ranked

1. **No demonstrated clipping, overflow, or missing table header in the inspected pages.** Page 1 contains the title, numbered references, a chronology table, and the beginning of the synthesis. Page 6's narrative fits inside the A4 margins. Pages 8–9 show separate article IOC and unconfirmed-lead lists with source-number links. Pages 10–13 show the technical inventory; rows wrap within their cells and headers repeat. Page 14 shows publication identity/provenance. The visible chronology figure is rendered as a table. No dropped figure or table can be established without the canonical input JSON.
2. **Editorial decision required — identity and hierarchy.** The preview has a generic `Publication` label and article-first layout. The RU/CN references have bulletin branding, issue/date identity, and contents pages. That difference is consistent with the different document types, but whether the preview should inherit bulletin identity or hierarchy needs an editorial decision.
3. **Editorial decision required — references and notes.** This PDF groups source records at the start and uses numbered links in narrative and IOC sections. Historical article pages use local footnote links. The inspected PDF has visible references and link annotations; no specific missing citation was demonstrated. Choosing a grouping or note style is editorial.
4. **Editorial decision required — tail pagination.** Page 13 ends with two inventory rows and page 14 contains only the provenance identity. The template explicitly page-breaks before provenance (`chpTypst/RENDERER/publication_helpers.typ:1201–1204`); the whitespace is a costly but intentional extension, not accidental table overflow. Do not compact it without approval.
5. **Unresolved figure completeness.** The supplied PDF shows the timeline as a captioned table. No canonical document JSON was provided, so expected diagrams/figures and completeness of the rendered section set remain `UNKNOWN`.

The successful synthetic render produced a three-page A4 PDF (Typst 0.15.1). Pages 1–3 were rendered and visually inspected: references, chronology, narrative/table content, both fixture figures, IOC sections, the technical inventory, and the separate provenance page were visible. The table cells and long values wrapped within their columns; no clipping, overflow, or missing mapped element was demonstrated. This structural and typography check supports the real-PDF observations without requiring pixel parity across different content.

`pdffonts` on the synthetic PDF shows embedded Hanken Grotesk Regular/Bold/ExtraBold/Italic and Cascadia Mono Regular, matching the type roles used in the reference templates. The muted teal heading/table hierarchy and title-first layout carry through; RU/CN approved PDFs add issue/date mastheads, contents cards, and article-page footnotes. The generic identity and start-of-document reference grouping are consistent with the editorial differences above. The code-only CHP/pipeline references have no approved output PDF for visual comparison.

The current path reads and parses canonical V4/V5/V6 publication documents in `publication_rendering.py:704–724`, projects their versioned content through `TypstRenderer.render` in `typst_rendering.py:798–853`, then compiles `RENDERER/publication.typ`. The projection unwraps V6 to its V5 base, retains V5 references/original indicators/IOC details, and accepts V4 directly (`typst_rendering.py:978–1016`). The compiler pins Typst 0.15.1, passes bundle font paths, ignores system fonts, and pins the creation timestamp (`typst_compiler.py:288–305`). No versioned entity or provenance extension was changed.

## Fonts and offline checks

`infra/typst-fonts.lock` names `chp-fonts-v1` and 102 required files, including Cascadia Code/Mono and Hanken Grotesk. With `FONT_BUNDLE_ROOT` unset, `typst_paths.py:14–20` defaults to this worktree's `chpTypst`; that gitignored directory is absent in this worktree. The lock-listed font bundle is present in the main checkout and was read in place, without copying assets into Git:

`/home/nill/perso/work/AutoWork/chpTypst`

The `FONT_BUNDLE_ROOT` snapshot loaded successfully against the worktree's tracked lock. The installed compiler was `/snap/typst/current/bin/typst`; the tests loaded the repository template bundle from this worktree. Temporary outputs were placed under `/home/nill/.cache/autowork-mission02/tmp`.

Checks actually run:

- `pdfinfo`, `pdftotext -layout`, `pdffonts`, page rendering with `pdftoppm`, and visual inspection of the pages listed above; equivalent selected-page inspection for RU2609 and CN2609.
- Real synthetic current-renderer test `test_real_typst_pdf_preserves_chp_publication_structure`: **1 passed**. `pdfinfo` reported 3 pages, A4, PDF 1.7, Creator `Typst 0.15.1`, 418,141 bytes. The generated PDF and inspection PNGs remained in temporary directories and were not committed.
- Versioned render test `test_real_typst_and_poppler_render_v6_opinion_without_changing_v5` passed in the 42-test suite. It compiled V5 before/after V6 augmentation, asserted byte-identical V5 PDFs, and compiled V6. The resulting synthetic V6 PDF is 2-page A4; both pages were visually inspected, including the analyst-opinion content and separate provenance page. No clipping or missing mapped content was observed.
- A separate 100-row, table-only Typst 0.15.1 diagnostic compiled offline to three pages and showed the repeated header on page 2. It used a system font and did not exercise the AutoWork renderer; the status above is established by the current-renderer test.
- `tests/test_typst_rendering.py`, `tests/test_typst_chp_parity.py`, and `tests/test_ci_typst_smoke_fixture.py`, invoked offline with Python 3.12.4 and the direct Typst 0.15.1 binary: **42 passed**, including the 12 tests that were initially blocked by the missing worktree bundle.
- Exact commands and environment (from `backend/`):

  ```sh
  TMPDIR=/home/nill/.cache/autowork-mission02/tmp FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst TYPST_FONTS_LOCK_FILE=/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s6-typst-real-pdf/infra/typst-fonts.lock TYPST_BINARY=/snap/typst/current/bin/typst PYTHONPATH=src python3 -m pytest tests/test_typst_chp_parity.py::test_real_typst_pdf_preserves_chp_publication_structure -q --tb=short --basetemp=/home/nill/.cache/autowork-mission02/tmp/pytest-m02-s6-target
  TMPDIR=/home/nill/.cache/autowork-mission02/tmp FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst TYPST_FONTS_LOCK_FILE=/home/nill/.paseo/worktrees/0y1qsxa1/mission02-s6-typst-real-pdf/infra/typst-fonts.lock TYPST_BINARY=/snap/typst/current/bin/typst PYTHONPATH=src python3 -m pytest tests/test_typst_rendering.py tests/test_typst_chp_parity.py tests/test_ci_typst_smoke_fixture.py -q --tb=short --basetemp=/home/nill/.cache/autowork-mission02/tmp/pytest-m02-s6-suite
  ```
- The direct offline pytest invocation succeeded without downloading dependencies. `uv run --python 3.12` could not prepare its environment offline: the default uv cache was read-only; with a `/tmp` cache, the required `numpy==2.5.4` wheel was unavailable.

No provider, enrichment, database, MinIO, Docker, or live application path was used. No test file or renderer patch was added because inspection demonstrated no rendering defect to correct; layout differences are recorded for editorial decision.

## Next permissible action

Resolve the identity, reference placement, and provenance pagination choices before making any style changes. No renderer correction is indicated by the inspected subject PDF or successful synthetic render.
