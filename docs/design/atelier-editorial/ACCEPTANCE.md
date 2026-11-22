# Atelier éditorial — acceptation M23 (F5)

Traceability of the M23 acceptance evidence. This document is a *drift guard
target*: `backend/tests/test_editorial_acceptance_traceability.py` parses every
backticked reference below and fails when a referenced file or test node no
longer exists. It is not itself behavioural evidence.

## How to read the levels

| Level | Meaning |
|---|---|
| **U** | Deterministic unit/contract test, no PostgreSQL, no network. |
| **PG** | Real PostgreSQL schema and SQLAlchemy unit of work; memory blob store. |
| **TYPST** | Real Typst binary compiles a real publication document to PDF; Poppler extracts the text order. |
| **VITEST** | React component/store test with the typed API client stubbed. |
| **E2E** | Playwright desktop spec against the **stubbed** API mode (no live backend). |

`E01`–`E22` are the M23 plan scenario identifiers. Where a frontend spec reuses
`E19`, `E20` or `E21` for a different UI scenario, both usages are listed and
the table says so; the backend proof for the plan's `E20`/`E21` must not be
inferred from the UI title alone.

## Running the oracles

```bash
# PG acceptance chain + behavioural tails (real Typst required)
export TYPST_BINARY=$HOME/.local/share/autowork-tools/typst
export PATH=$HOME/.local/share/autowork-tools/bin:$PATH
export TMPDIR=$HOME/.cache/m06tmp
export FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst
flock /tmp/m23-pg.lock make test-integration \
  INTEGRATION_TEST_PATH=tests/integration/editorial/test_acceptance_chain_postgres.py \
  INTEGRATION_PYTEST_ARGS="-q --tb=short -p no:cacheprovider"

# Backend unit + real-Typst editorial suite
cd backend && PYTHONPATH=$PWD/src .venv/bin/python -m pytest \
  tests/test_editorial_*.py -q --tb=short -p no:cacheprovider

# Traceability drift guard
cd backend && PYTHONPATH=$PWD/src .venv/bin/python -m pytest \
  tests/test_editorial_acceptance_traceability.py -q --tb=short -p no:cacheprovider

# UI component tests and desktop e2e (stubbed API)
cd frontend && pnpm test --run
cd frontend && pnpm exec playwright test --config playwright.editorial.config.ts
```

## Acceptance matrix

| ID | Behaviour (plan wording) | Backend / Typst proof | UI proof | Level | Honest limits |
|---|---|---|---|---|---|
| E01 | Manual text edit is saved canonically without a Bridge/model call and the derived PDF becomes STALE | `backend/tests/integration/editorial/test_manual_edit_postgres.py::test_manual_edit_real_route_persists_replays_and_refuses_unsupported_values`; `backend/tests/test_editorial_manual_patch.py::test_manual_edit_makes_zero_gateway_calls`; `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_acceptance_chain_postgres` | `frontend/e2e/editorial/text-ux-contract.spec.ts::E01 edit and save canonically without a Bridge call and mark the PDF stale`; `frontend/e2e/editorial/text-ux-contract.spec.ts::E01b the lead paragraph saves through rewrite_lead` | PG + E2E | The chain asserts zero new `model_runs` rows and a failing gateway double; the E2E API is stubbed. |
| E02 | A local draft survives a block switch and resumes without saving | `backend/tests/test_editorial_drafts.py::test_draft_subject_acl_and_version_fence` proves the *server* draft contract only | `frontend/src/features/editorial-workspace/InlineTextEditor.test.tsx::test_switching_blocks_keeps_draft_by_block_id`; `frontend/e2e/editorial/text-ux-contract.spec.ts::E02 a local draft survives a block switch and is resumed without a save` | U + VITEST + E2E | The browser draft is React-memory only; no backend persistence is claimed for it. |
| E03 | A Bridge proposal compares, accepts without a model call and keeps the block | `backend/tests/test_editorial_proposal_preparation.py::test_bridge_patch_becomes_immutable_proposal_with_exact_sha`; `backend/tests/integration/editorial/test_proposal_adoption_postgres.py::test_accept_adopts_enrichment_table_replacement_and_advances_registry` | `frontend/e2e/editorial/text-ux-contract.spec.ts::E03 a Bridge proposal compares, accepts without a model call, and keeps the block` | U + PG + E2E | The Bridge **transport** is not exercised; preparation uses fake provider doubles. |
| E04 | Two identical paragraphs keep independent identities and drafts | `backend/tests/test_editorial_workspace_snapshot.py::test_legacy_bootstrap_is_stable_and_duplicate_ids_differ`; `backend/tests/test_editorial_workspace_snapshot.py::test_duplicate_paragraphs_resolve_to_distinct_canonical_paths` | `frontend/e2e/editorial/text-ux-contract.spec.ts::E04 two identical paragraphs keep independent identities and drafts` | U + E2E | UI identity rendering is stubbed-API only. |
| E05 | Bridge element threads offer typed visual corrections and reconcile exactly once | `backend/tests/integration/editorial/test_bridge_visual_revision_postgres.py::test_bridge_caption_reconcile_prepares_exactly_one_proposal_and_adopts_it`; `backend/tests/test_editorial_bridge_visual_revision.py::test_visual_correction_turn_is_prepared_once_after_reconcile`; `backend/tests/test_editorial_bridge_visual_revision.py::test_chart_label_correction_keeps_one_chart_and_is_evidenced` | — | U + PG | Bridge replies are fake provider doubles; real transport, network retries and Bridge-side limits are **not proven**. |
| E06 | Chart correction compares current/proposed values and adopts exactly one chart | `backend/tests/integration/editorial/test_enrichment_revision_route_postgres.py::test_route_revise_chart_prepare_accept_end_to_end`; `backend/tests/test_editorial_chart_revision_typst.py::test_typst_pdf_renders_only_the_revised_chart_caption` | `frontend/e2e/editorial/visual-actions.spec.ts::E06 — chart correction proposal compares current and proposed values, then adopts exactly one chart` | PG + TYPST + E2E | The Typst test asserts text/SVG content and absence of the old caption, not full visual parity. |
| E07 | Evidence-backed bar charts use exact values/units from admitted records | `backend/tests/test_editorial_bar_chart.py::test_bar_data_requires_finite_evidence_backed_values`; `backend/tests/test_editorial_bar_chart.py::test_bar_point_requires_value_and_unit_in_the_same_admitted_evidence`; `backend/tests/integration/editorial/test_table_insertion_postgres.py::test_manual_bar_chart_patch_accepts_with_registered_evidence_and_anchor` | — | U + PG | Local SVG rendering only; no external chart service. |
| E08 | A chart value without same-record proof is refused without a canonical write | `backend/tests/test_editorial_bridge_visual_revision.py::test_chart_value_without_same_record_proof_is_refused`; `backend/tests/integration/editorial/test_enrichment_chart_diagram_revision_postgres.py::test_chart_value_without_admitted_proof_is_refused_without_a_canonical_write`; `backend/tests/integration/editorial/test_bridge_visual_revision_postgres.py::test_bridge_chart_unproven_value_is_refused_without_canonical_change` | — | U + PG | Refusal codes are closed; the exact Bridge wire form is exercised with fake replies only. |
| E09 | Gallery filters by source, previews without losing selection, and integrates in order | `backend/tests/test_editorial_figure_catalog.py::test_opaque_figure_id_is_scoped_to_subject_and_current_run`; `backend/tests/test_editorial_figure_catalog.py::test_ineligible_figure_exposes_reason_and_is_not_selectable` | `frontend/src/features/editorial-workspace/SourceFigureGallery.test.tsx::shows eligibility reasons and keeps selection local while previewing`; `frontend/e2e/editorial/gallery-replace.spec.ts::E09 — galerie, filtre par source, aperçu sans perte de sélection et intégration ordonnée` | U + VITEST + E2E | Gallery E2E uses fixtures, not a live catalogue. |
| E10 | Moving an image from paragraph 2 to paragraph 5 sends the exact anchor and never calls Bridge | `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_acceptance_chain_postgres` (move step); `backend/tests/integration/editorial/test_enrichment_revision_route_postgres.py::test_route_move_prepare_accept_end_to_end`; `backend/tests/integration/editorial/test_enrichment_visual_revision_postgres.py::test_move_changes_only_placement_and_records_history_and_impact` | `frontend/e2e/editorial/visual-actions.spec.ts::E10 — moving an image to paragraph 5 previews the destination, sends the exact anchor and never calls Bridge` | PG + TYPST + E2E | The chain compiles the moved-element PDF and asserts its text order. |
| E11 | Swapping two figures sends the exact permutation and survives reload | `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_acceptance_chain_postgres` (reorder step); `backend/tests/integration/editorial/test_enrichment_revision_route_postgres.py::test_route_reorder_prepare_accept_end_to_end`; `backend/tests/integration/editorial/test_enrichment_visual_revision_postgres.py::test_reorder_permutation_under_lock_and_reorder_vs_create_one_winner` | `frontend/e2e/editorial/visual-actions.spec.ts::E11 — swapping two images sends the exact permutation and survives a reload` | PG + TYPST + E2E | "Survives reload" is proven by re-fetching the server snapshot and by a fresh Typst render. |
| E12 | Replacement is compared, confirmed by one typed revision and keeps the old archive | `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_acceptance_chain_postgres` (replace step); `backend/tests/integration/editorial/test_enrichment_revision_route_postgres.py::test_route_replace_source_figure_prepare_accept_end_to_end`; `backend/tests/integration/editorial/test_enrichment_replace_remove_postgres.py::test_replace_source_figure_is_in_place_and_archive_safe` | `frontend/e2e/editorial/gallery-replace.spec.ts::E12 — remplacement comparé, confirmé par une seule révision typée` | PG + TYPST + E2E | Chain asserts exactly one enrichment version bump and one PDF caption. |
| E13 | Removal confirms, archives nothing less, and adopts the delete | `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_acceptance_chain_postgres` (remove step, archive SHA equality); `backend/tests/integration/editorial/test_enrichment_replace_remove_postgres.py::test_remove_enrichment_element_invalidates_render_and_keeps_archive`; `backend/tests/integration/editorial/test_enrichment_visual_revision_postgres.py::test_stale_proposal_cannot_be_accepted_and_reject_leaves_canonical_unchanged` | `frontend/e2e/editorial/visual-actions.spec.ts::E13 — removal asks for confirmation, archives nothing less, and adopts the delete` | PG + TYPST + E2E | The chain asserts every old canonical artifact blob keeps its SHA-256 and remains parseable. |
| E14 | Direct caption save and Bridge correction are distinct paths | `backend/tests/integration/editorial/test_enrichment_revision_route_postgres.py::test_route_caption_prepare_accept_end_to_end`; `backend/tests/test_editorial_bridge_visual_revision.py::test_caption_correction_is_prepared_as_a_typed_visual_patch`; `backend/tests/integration/editorial/test_enrichment_visual_revision_postgres.py::test_caption_only_change_preserves_figure_archive_and_original_caption` | `frontend/e2e/editorial/visual-actions.spec.ts::E14 — direct caption save and Bridge correction are distinct paths` | U + PG + E2E | Bridge path uses fake replies; manual path is fully wired on PG. |
| E15 | BRIEF refuses more than three integrated figures; ARTICLE has no such cap | `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_acceptance_chain_postgres` (named `brief_figure_limit_exceeded` + capability withdrawal); `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_article_integrates_four_figures_postgres`; `backend/tests/test_editorial_visual_revision_figure.py::test_replace_keeps_the_brief_figure_cap` | `frontend/e2e/editorial/gallery-replace.spec.ts::E15 — plafond BRIEF expliqué et quatrième sélection bloquée`; `frontend/e2e/editorial/gallery-replace.spec.ts::E15 — ARTICLE sans plafond de figures` | PG + E2E | The named ceiling is proven at prepare by a two-figure request at count two; at count three the server withdraws `create_source_figure` and a forged single request is refused. |
| E16 | A concurrent accept conflict is explicit and leaves the document unchanged | `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_concurrent_accept_has_one_winner_postgres`; `backend/tests/integration/editorial/test_enrichment_visual_revision_postgres.py::test_move_vs_concurrent_edit_has_exactly_one_winner`; `backend/tests/integration/editorial/test_enrichment_replace_remove_postgres.py::test_remove_vs_concurrent_replace_has_exactly_one_winner` | `frontend/e2e/editorial/visual-actions.spec.ts::E16 — an accept conflict is explicit and leaves the document unchanged` | PG + E2E | The thread accept route exposes the adoption writer's closed code (`proposal_state_conflict` / `proposal_stale` / `proposal_base_stale`); exactly one effect and one version bump are asserted. |
| E17 | Rejecting a visual proposal leaves the canonical chart unchanged | `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_rejected_visual_proposal_leaves_canonical_unchanged_postgres`; `backend/tests/integration/editorial/test_enrichment_visual_revision_postgres.py::test_stale_proposal_cannot_be_accepted_and_reject_leaves_canonical_unchanged` | `frontend/e2e/editorial/visual-actions.spec.ts::E17 — rejecting a visual proposal leaves the canonical chart unchanged` | PG + E2E | Chain asserts the exact canonical payload SHA is unchanged and no publication version is created. |
| E18 | Plan navigation scrolls to and keeps the active block, including after history | — | `frontend/e2e/editorial/text-ux-contract.spec.ts::E18 plan navigation scrolls to and keeps the active block, including after history` | E2E | UI-only scenario; no backend claim. |
| E19 | Keyboard-only select/edit/save/compare/accept and gallery workflows keep focus | — | `frontend/e2e/editorial/text-ux-contract.spec.ts::E19 keyboard-only select, edit, save, compare and accept keeps focus`; `frontend/e2e/editorial/visual-actions.spec.ts::E19 — keyboard-only: select a visual, enter move mode, choose an anchor, confirm`; `frontend/e2e/editorial/gallery-replace.spec.ts::E19 — sélection de galerie et remplacement au clavier, aperçu sans perte de sélection`; `frontend/src/features/editorial-workspace/ReplaceFigureDialog.test.tsx::selects and confirms with the keyboard only`; `frontend/src/features/editorial-workspace/FigureInsertionPanel.test.tsx::previews the ordered selection in the document and reorders with the keyboard` | VITEST + E2E | UI-only scenario; the axe audit remains out of scope (accepted risk). |
| E20 | One shared canonical order drives the workshop, publication input and the PDF after a move from the Visuels tab | `backend/tests/test_editorial_enrichment_order_parity.py::test_snapshot_publication_and_typst_share_the_canonical_element_order`; `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_acceptance_chain_postgres` (every step compares snapshot order, resolved publication input and real PDF text order) | `frontend/e2e/editorial/gallery-replace.spec.ts::E20 — déplacement depuis l’onglet Visuels: panneau inspecteur, ancrage et confirmation` | TYPST + E2E | The Typst proof is structural/text order, not page-perfect visual parity; page images are not compared. |
| E21 | A historical publication version is read-only and non-mutating | `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_historical_publication_version_is_read_only_postgres`; `backend/tests/integration/editorial/test_legacy_read_only_postgres.py::test_legacy_read_only_postgres_keeps_artifact_bytes_and_versions`; `backend/tests/integration/editorial/test_legacy_read_only_postgres.py::test_forged_write_on_read_only_context_writes_no_proposal_or_revision` | `frontend/e2e/editorial/gallery-replace.spec.ts::E21 — fermer le remplacement ou le déplacement rend le focus au contrôle d’origine` (same ID, different UI scenario) | PG + E2E | The workspace historical view uses the artifact-version route and reports `read_only_reason=historical_version`; a write through a historical snapshot id is refused (`manual_edit_stale`/`editorial_snapshot_stale`). The exact `editorial_read_only` code is proven for a read-only **current** context by the legacy PG oracle; no editorial write route targets a historical artifact. |
| E22 | Diagram correction keeps one diagram, recompiles the asset and retains the old one | `backend/tests/integration/editorial/test_acceptance_chain_postgres.py::test_manual_diagram_revision_route_postgres`; `backend/tests/test_editorial_visual_revision_diagram.py::test_label_only_diagram_revision_is_accepted_and_recompiled`; `backend/tests/integration/editorial/test_bridge_visual_revision_postgres.py::test_bridge_diagram_label_correction_adopts_one_diagram`; `backend/tests/integration/editorial/test_enrichment_chart_diagram_revision_postgres.py::test_diagram_revision_recompiles_and_retains_the_old_asset` | — | U + PG | D2 compilation is local/deterministic; no UI diagram editor exists in the current workspace surface. |

## What is deliberately **not** proven

- **Real Bridge transport.** Every Bridge path above uses the fake provider
  doubles. No network call, retry or Bridge-side size/serialisation behaviour is
  exercised.
- **Live provider/gateway calls.** All PG oracles use `_NoModelCalls`; the
  acceptance chain additionally baselines `model_runs` and asserts no growth.
- **UI against the real backend.** Playwright specs run in stubbed API mode; the
  PG chain proves the backend separately. No end-to-end browser-to-PostgreSQL
  test exists.
- **Visual parity of the PDF.** The Typst proofs assert successful compilation,
  caption presence/absence and extracted text order. They do not compare
  rendered pages to approved historical templates (`autowork-typst-parity`
  remains the reference for that, with human signoff).
- **Concurrent processes.** E16 races two requests in one event loop against the
  real database; it exercises the run lock and CAS fences, not OS-level
  multi-process scheduling.
- **Mobile.** The workshop is desktop-only by decision (ADR-0010 D9); no mobile
  criterion is claimed.

## Defect found by F5 and fixed in F5b

- **BRIEF overview merge loses `after_paragraph` enrichments anchored to an
  overview paragraph.** `brief_publication_builder` merges the OVERVIEW section
  into `document.lead`. `typst_rendering` derived the insertion key from the
  synthesis anchor (`section-0-paragraph-N`), while the merged paragraph is
  emitted as `lead-paragraph-N`; the element could be silently absent from the
  PDF or inserted after a different section's paragraph, and `after_lead`
  elements were ordered before the overview block in the workshop snapshot.

  Fixed by `merge_brief_narrative_sections` in
  `backend/src/cti_app/application/brief_publication_builder.py`: the single
  merge authority now returns the synthesis-paragraph-to-publication-body map
  consumed by `PublicationAssemblyService`, so `resolved_placements` carry the
  merged publication identity (`/lead/N` or the re-indexed
  `/sections/S/paragraphs/P`). The workshop projection uses the same overview
  definition to place `after_lead` elements after the merged block; a BRIEF
  placement whose anchor cannot be mapped, or whose resolved anchor is never
  emitted, now refuses the assembly/render (`editorial_anchor_stale`) instead
  of dropping the element. ARTICLE behavior, the publication schemas and
  historical artifacts/hashes are unchanged.

  Proven by `backend/tests/test_editorial_brief_overview_anchor_parity.py::test_brief_overview_anchors_render_after_their_paragraphs`
  (snapshot order, resolved placement order and real-Typst PDF order agree),
  `::test_brief_ambiguous_merged_anchor_is_refused_not_dropped` (assembly
  refusal) and `::test_brief_unemitted_resolved_placement_raises` (render
  refusal). The PG acceptance chain keeps its TECHNICAL-section fixture and
  still passes (`test_acceptance_chain_postgres`, 10 passed).

## Production defect found and fixed by F5

- **Snapshot schema rejected non-paragraph charts.** `EditorialWorkspaceBarChart`
  required `anchor_block_id: UUID`, but legacy/model-driven enrichments legitimately
  place a chart after the lead, a section, the timeline or at the end (anchor
  `None`). `GET /api/subjects/{id}/editor/snapshot` returned a 500 for such a
  document. The field is now `UUID | None`, matching the table/figure/diagram
  projections; `test_manual_diagram_revision_route_postgres` exercises the route
  with an `after_lead` chart.
