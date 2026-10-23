# Design documents — current contracts vs historical evidence

**Index only; verified at Git `2b3df6d286cc364a17e1d5072f3b1248bd836394` on 2026-10-23.**
Project instructions and the accepted ADRs in `../adr/`, actual code and
versioned migrations govern current work. This directory is **not** a queue
of instructions for an agent to execute.

## Task-specific design references — inspect current implementation

These documents describe potentially useful module interfaces or decisions;
they are not proof that the deployed run conforms to them.

- Goodware / analysis contracts:
  [07-goodware-index-v2.md](07-goodware-index-v2.md),
  [08B-code-feature-assessment-boundary.md](08B-code-feature-assessment-boundary.md),
  [09-invariant-registry.md](09-invariant-registry.md),
  [09-invariant-thresholds.md](09-invariant-thresholds.md),
  [10-proposal-conversation.md](10-proposal-conversation.md).
  The previously unused P09 example thresholds file has been deleted; the
  completed threshold decision remains.
- Evidence and editorial interfaces:
  [2026-10-08-source-evidence-layout-canonicalization.md](2026-10-08-source-evidence-layout-canonicalization.md),
  [2026-10-08-livrable-export-ioc-sommaire.md](2026-10-08-livrable-export-ioc-sommaire.md),
  [2026-10-08-targeted-editorial-revision.md](2026-10-08-targeted-editorial-revision.md),
  [2026-10-08-targeted-revision-agent-interface.md](2026-10-08-targeted-revision-agent-interface.md),
  [2026-10-12-agent-control-view.md](2026-10-12-agent-control-view.md),
  [2026-10-12-pdf-inspection-api.md](2026-10-12-pdf-inspection-api.md),
  [2026-10-12-typst-input-inspection.md](2026-10-12-typst-input-inspection.md).
- Other development snapshots and the `cti-recovery-*` notes are not
  promoted to current contracts by appearing in this folder; validate
  every assertion against source and accepted ADRs before using it.

## Historical run records and implementation evidence — preserve

Retain dated run audits, journals, measured fixtures and recovery proofs,
including `2026-10-08-chantier-correction-journal.md`,
`2026-10-08-source-criticality-real-run.md`,
`2026-10-12-chantier-pre-pivot-journal.md`,
`2026-10-12-exclusion-and-workflow-emulator-journal.md`,
`2026-10-12-multibatch-composition-proof-journal.md`,
`2026-10-13-investigation-agent-journal.md`,
`cti-recovery-l0-status.md`, and
`real-run-cti-quality-recovery-plan.md`. They document earlier facts,
**not** current authorization or fresh runtime validation.
See also `../operations/run12-editorial-closure-2026-10-22.md`.
Keep contradictory dated evidence intact and qualify it by **date and SHA**.

In particular, `2026-10-12-edition-composition-adr.md` is a dated
composition design record; its pre-ADR-0007 migration/restamp claims
are **superseded**. The active schema authority is
[ADR-0007](../adr/ADR-0007-frozen-baseline-and-c2-migration.md).

## Obsolete documents removed

Ten superseded/duplicate files were removed in the 2026-10-23 cleanup.
Their original content remains available at the immutable
[pre-cleanup Git revision](https://github.com/m413w4r3/AutoWork/tree/2b3df6d286cc364a17e1d5072f3b1248bd836394).
The removal rationale and each path are indexed in
[the documentation authority index](../README.md).

## Mission 00

The forthcoming real-run audit is read-only and must not execute old
design plans. Start from `../README.md`, the current AGENTS instructions
and `../operations/run12-editorial-closure-2026-10-22.md`;
qualify offline tests separately from persisted and live observations.
