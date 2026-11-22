import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/editions";
import type {
  EditorialFigureCatalogItem,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import {
  briefFigureCapacity,
  figureExclusionReason,
  figureSelectionLabel,
  figureSourceLabel,
  replaceFigureErrorInfo,
  resolveReplaceFigureTarget,
} from "./replaceFigureContract";

const ANCHOR_ID = "11111111-1111-4111-8111-111111111111";
const FIGURE_BLOCK_ID = "22222222-2222-4222-8222-222222222222";
const ELEMENT_KEY = "adopted_source_figure_aaaa";

function anchorBlock(): EditorialWorkspaceBlock {
  return {
    block_id: ANCHOR_ID,
    kind: "paragraph",
    parent_block_id: null,
    position: 0,
    text: "Paragraphe d’ancrage synthétique utilisé pour la figure.",
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: ANCHOR_ID, legacy_target_id: null },
    before_hash: "a".repeat(64),
    allowed_operations: ["rewrite_paragraph"],
    revision_status: "ready",
  };
}

function figureBlock(
  overrides: Partial<EditorialWorkspaceBlock> = {},
): EditorialWorkspaceBlock {
  return {
    block_id: FIGURE_BLOCK_ID,
    kind: "figure",
    parent_block_id: null,
    position: 1,
    text: "Légende actuelle",
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: FIGURE_BLOCK_ID, legacy_target_id: null },
    before_hash: "b".repeat(64),
    allowed_operations: ["replace_source_figure"],
    revision_status: "ready",
    element_kind: "figure",
    element_key: ELEMENT_KEY,
    caption: "Légende actuelle",
    figure: {
      key: ELEMENT_KEY,
      catalog_figure_id: "33333333-3333-4333-8333-333333333333",
      caption: "Légende actuelle",
      source_url: "https://source.example/report",
      source: "Rapport synthétique",
      provenance: "Figure archivée et validée.",
      sha256: "c".repeat(64),
      mime_type: "image/png",
      byte_size: 1024,
      width: 800,
      height: 450,
      status: "included",
      order: 0,
      anchor_block_id: ANCHOR_ID,
      placement: "after_paragraph",
      page: 3,
      section: "Résultats",
    },
    ...overrides,
  };
}

function snapshot(
  overrides: Partial<EditorialWorkspaceSnapshot> = {},
): EditorialWorkspaceSnapshot {
  return {
    subject_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    publication_kind: "article",
    synthesis_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    synthesis_version: 3,
    synthesis_canonical_sha256: "d".repeat(64),
    enrichment_artifact_id: null,
    enrichment_version: null,
    publication_artifact_id: null,
    publication_version: null,
    pipeline_generation: 1,
    blocks: [anchorBlock(), figureBlock()],
    enrichments: [],
    document_outline: [],
    capabilities: {
      can_discuss: false,
      can_preview_revision: false,
      manual_edit: false,
      can_add_enrichment: false,
      can_preview_pdf: false,
      can_regenerate_pdf: false,
    },
    preview_status: "READY",
    impact_status: "none",
    pdf_status: "CURRENT",
    pdf_artifact_id: null,
    pdf_artifact_version: null,
    pdf_artifact_input_hash: null,
    pdf_artifact_canonical_sha256: null,
    pdf_render_id: null,
    pdf_render_identity: null,
    pdf_error_code: null,
    pdf_error_message: null,
    pdf_url: null,
    snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    ...overrides,
  };
}

function catalogItem(
  overrides: Partial<EditorialFigureCatalogItem> = {},
): EditorialFigureCatalogItem {
  return {
    figure_id: "44444444-4444-4444-8444-444444444444",
    original_name: "Carte des flux",
    original_caption: "Vue synthétique.",
    alt_text: null,
    source: { source_id: "uuid-source", title: null, url: null },
    page: null,
    section: null,
    context_before: null,
    context_after: null,
    width: 800,
    height: 450,
    mime_type: "image/png",
    status: "available",
    selectable: true,
    exclusion_reason: null,
    already_used: false,
    ...overrides,
  };
}

describe("replaceFigureContract", () => {
  it("resolves an integrated figure by opaque element key and exposes its anchor", () => {
    const target = resolveReplaceFigureTarget(snapshot(), ELEMENT_KEY);
    expect(target).not.toBeNull();
    expect(target?.blockId).toBe(FIGURE_BLOCK_ID);
    expect(target?.elementKey).toBe(ELEMENT_KEY);
    expect(target?.anchorText).toBe(
      "Paragraphe d’ancrage synthétique utilisé pour la figure.",
    );
    expect(target?.currentFigure.caption).toBe("Légende actuelle");
  });

  it("never resolves an unknown or non-figure element key", () => {
    expect(resolveReplaceFigureTarget(snapshot(), "unknown")).toBeNull();
    expect(
      resolveReplaceFigureTarget(
        snapshot({
          blocks: [anchorBlock()],
          enrichments: [],
        }),
        ELEMENT_KEY,
      ),
    ).toBeNull();
  });

  it("uses closed-code French copy for replacement refusals", () => {
    expect(replaceFigureErrorInfo(new Error("boom")).message).toContain(
      "Vérifiez la connexion",
    );
    expect(replaceFigureErrorInfo(new Error("boom")).refresh).toBe(false);
    const stale = replaceFigureErrorInfo(
      new ApiError("stale", "enrichment_target_stale", 409),
    );
    expect(stale.code).toBe("enrichment_target_stale");
    expect(stale.message).toContain("Actualisez l’atelier");
    expect(stale.refresh).toBe(true);
    const revoked = replaceFigureErrorInfo(
      new ApiError("missing", "editorial_figure_not_found", 404),
    );
    expect(revoked.message).toContain("retirée ou révoquée");
    expect(revoked.refresh).toBe(true);
    const unsupported = replaceFigureErrorInfo(
      new ApiError(
        "unsupported",
        "enrichment_revision_unsupported_operation",
        422,
      ),
    );
    expect(unsupported.message).toContain("pas encore disponible");
    expect(unsupported.refresh).toBe(false);
  });

  it("labels sources without exposing UUIDs and explains excluded figures", () => {
    expect(figureSourceLabel(catalogItem())).toBe("Source archivée");
    expect(
      figureSourceLabel(
        catalogItem({
          source: { source_id: "uuid-source", title: "Rapport", url: null },
        }),
      ),
    ).toBe("Rapport");
    expect(
      figureExclusionReason(
        catalogItem({ status: "already_integrated", selectable: false }),
      ),
    ).toContain("déjà intégrée");
    expect(
      figureExclusionReason(
        catalogItem({
          status: "ineligible",
          selectable: false,
          exclusion_reason: "Licence non compatible.",
        }),
      ),
    ).toBe("Licence non compatible.");
  });

  it("computes the BRIEF remaining capacity from integrated figures", () => {
    const brief = snapshot({ publication_kind: "brief" });
    expect(briefFigureCapacity(brief)).toBe(2);
    const emptyBrief = snapshot({
      publication_kind: "brief",
      blocks: [anchorBlock()],
    });
    expect(briefFigureCapacity(emptyBrief)).toBe(3);
    const threeFigures = snapshot({
      publication_kind: "brief",
      blocks: [
        anchorBlock(),
        figureBlock(),
        figureBlock({ block_id: "figure-2" }),
        figureBlock({ block_id: "figure-3" }),
      ],
    });
    expect(briefFigureCapacity(threeFigures)).toBe(0);
    expect(briefFigureCapacity(snapshot())).toBeNull();
  });

  it("formats the selection counter in French", () => {
    expect(figureSelectionLabel(0)).toBe("0 images sélectionnées");
    expect(figureSelectionLabel(1)).toBe("1 image sélectionnée");
    expect(figureSelectionLabel(3)).toBe("3 images sélectionnées");
  });
});
