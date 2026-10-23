---
name: autowork-typst-parity
description: Assess structural and visual parity between current Typst renders and approved historical templates without changing canonical CTI evidence.
---

Inputs: approved reference files, fonts/Typst version, same-content
fixtures, current renderer and hashes. If reference archives or PDF are
missing, return BLOCKED_MISSING_REFERENCE, not a guessed comparison.

Verify article structure, footnotes, typography, headings, tables, figure
captions, citations, IOC labels and semantic distinction between sourced,
confirmed, candidate, excluded and analyst observation. Compile/render
offline with controlled versions and compare representative pages; text
extraction or successful compilation alone is not visual parity.
Classify each observed discrepancy RESTORE_REFERENCE,
KEEP_JUSTIFIED_EXTENSION or DEFER; record page, SHA, reproducer, test
and human signoff needed. No live publication, release or G3 operation.
