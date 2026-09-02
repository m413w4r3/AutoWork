# Pandoc reference documents

`reference-doc-v2.docx` is the reference document used by every DOCX export
(`cti_app.application.pandoc_export.DEFAULT_REFERENCE_DOC`).

`reference-doc-v1.docx` is kept as the historical artifact it was derived from.
It still carries the editorial template's own metadata (`Bulletin n°32`,
`Juillet 2024`, `XXX`/`XXXX`) inside its headers and footers, which is exactly
the defect v2 fixes.  Do not point the exporter back at it.

## Regenerating

    uv run --python 3.12 --project backend \
        python scripts/generate_pandoc_reference_doc.py <template.docx> \
        backend/assets/pandoc/reference-doc-v2.docx

The script drops the document body (Pandoc owns the content) and rewrites the
header/footer metadata into placeholders using shape rules, never historical
values.  Running it on an already generated reference document is a no-op.

## Placeholders

| Placeholder            | Resolved from                                   |
| ---------------------- | ----------------------------------------------- |
| `{{EDITION_MONTH}}`    | `edition.period_start`, French month and year    |
| `{{EDITION_COUNTRY}}`  | `edition.country`                                |
| `{{BULLETIN_NUMBER}}`  | nothing yet — resolves to an empty string        |

`{{BULLETIN_NUMBER}}` spans the whole `Bulletin n°NN` label so that it can
disappear cleanly: no bulletin numbering exists in the domain, the API or the
assembly metadata, and it must not be fabricated from a version, a UUID or a
publication index.  Wiring a real number is still to be done.

Resolution happens in `cti_app.application.docx_postprocessing`, on the
header/footer parts only, after Pandoc has produced the DOCX.
