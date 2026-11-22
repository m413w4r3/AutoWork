import { describe, expect, it } from "vitest";

import type {
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import {
  canEditVisualCaption,
  canMoveVisualElement,
  canRemoveVisualElement,
  canReorderVisualElements,
  canReplaceVisualElement,
  canRequestVisualBridgeCorrection,
  isVisualElementBlock,
  paragraphExcerpt,
  visualBridgeOperation,
  visualElementAnchorId,
  visualElementIdentity,
  visualElementRefs,
  visualElementsSharingAnchor,
  visualElementTitle,
  visualRemoveOperation,
} from "./visualElementOperations";

function visualBlock(
  overrides: Partial<EditorialWorkspaceBlock> &
    Pick<EditorialWorkspaceBlock, "block_id" | "kind">,
): EditorialWorkspaceBlock {
  return {
    parent_block_id: null,
    position: 0,
    text: null,
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: overrides.block_id, legacy_target_id: null },
    before_hash: "a".repeat(64),
    allowed_operations: [],
    revision_status: "ready",
    ...overrides,
  };
}

const chart = visualBlock({
  block_id: "chart-block",
  kind: "chart",
  element_kind: "chart",
  element_key: "chart-a",
  order: 0,
  allowed_operations: [
    "rewrite_chart",
    "delete_chart",
    "update_visual_caption",
    "move_enrichment_element",
    "reorder_enrichment_elements",
  ],
  chart: {
    key: "chart-a",
    kind: "bar",
    title: "Synthetic chart",
    caption: "Synthetic caption",
    legend: [],
    category_axis_label: "Catégories",
    value_axis_label: "Valeurs",
    unit: "events",
    points: [],
    anchor_block_id: "anchor-1",
  },
});

const figureA = visualBlock({
  block_id: "figure-a-block",
  kind: "figure",
  element_kind: "figure",
  element_key: "figure-a",
  order: 1,
  allowed_operations: [
    "replace_source_figure",
    "delete_source_figure",
    "update_visual_caption",
    "move_enrichment_element",
    "reorder_enrichment_elements",
  ],
  figure: {
    key: "figure-a",
    catalog_figure_id: null,
    original_caption: "Original caption",
    caption: "Figure A",
    source_url: "https://example.invalid/source",
    source: "Synthetic source",
    provenance: "Synthetic provenance",
    sha256: "b".repeat(64),
    mime_type: "image/png",
    byte_size: 10,
    width: 10,
    height: 10,
    status: "included",
    order: 1,
    anchor_block_id: "anchor-1",
    placement: "after_paragraph",
    page: 1,
    section: "Section",
  },
});

const outdatedFigure = visualBlock({
  block_id: "figure-b-block",
  kind: "figure",
  element_kind: "figure",
  element_key: "figure-b",
  order: null,
  revision_status: "stale",
  allowed_operations: [],
  figure: {
    ...figureA.figure!,
    key: "figure-b",
    caption: "Figure B",
    order: null,
    anchor_block_id: "anchor-1",
  },
});

function snapshotWith(
  blocks: EditorialWorkspaceBlock[],
): EditorialWorkspaceSnapshot {
  return {
    blocks,
    enrichments: blocks,
  } as EditorialWorkspaceSnapshot;
}

describe("visualElementOperations", () => {
  it("only treats server-identified enrichment occurrences as visual elements", () => {
    expect(isVisualElementBlock(chart)).toBe(true);
    expect(
      isVisualElementBlock(
        visualBlock({ block_id: "para", kind: "paragraph", text: "Text" }),
      ),
    ).toBe(false);
    expect(
      isVisualElementBlock(
        visualBlock({ block_id: "legacy-figure", kind: "figure" }),
      ),
    ).toBe(false);
    expect(visualElementIdentity(chart)).toEqual({
      kind: "chart",
      key: "chart-a",
    });
  });

  it("maps element kinds to removal and Bridge operations", () => {
    expect(visualRemoveOperation("table")).toBe("delete_table");
    expect(visualRemoveOperation("chart")).toBe("delete_chart");
    expect(visualRemoveOperation("diagram")).toBe("delete_diagram");
    expect(visualRemoveOperation("figure")).toBe("delete_source_figure");
    expect(visualBridgeOperation("chart")).toBe("rewrite_chart");
    expect(visualBridgeOperation("diagram")).toBe("rewrite_diagram");
    expect(visualBridgeOperation("figure")).toBe("update_visual_caption");
  });

  it("gates every action on allowed_operations", () => {
    expect(canEditVisualCaption(chart)).toBe(true);
    expect(canMoveVisualElement(chart)).toBe(true);
    expect(canReorderVisualElements(chart)).toBe(true);
    expect(canReplaceVisualElement(chart)).toBe(false);
    expect(canReplaceVisualElement(figureA)).toBe(true);
    expect(canRemoveVisualElement(chart)).toBe(true);
    expect(canRequestVisualBridgeCorrection(chart)).toBe(true);

    const readOnly = { ...chart, allowed_operations: [] };
    expect(canEditVisualCaption(readOnly)).toBe(false);
    expect(canMoveVisualElement(readOnly)).toBe(false);
    expect(canReorderVisualElements(readOnly)).toBe(false);
    expect(canRemoveVisualElement(readOnly)).toBe(false);
    expect(canRequestVisualBridgeCorrection(readOnly)).toBe(false);
  });

  it("reads the anchor from the typed projection and a readable title", () => {
    expect(visualElementAnchorId(chart)).toBe("anchor-1");
    expect(visualElementAnchorId(figureA)).toBe("anchor-1");
    expect(visualElementTitle(chart)).toBe("Synthetic chart");
    expect(visualElementTitle(figureA)).toBe("Figure A");
  });

  it("lists only ready elements sharing the anchor in canonical order", () => {
    const blocks = [chart, figureA, outdatedFigure];
    expect(
      visualElementsSharingAnchor(snapshotWith(blocks), "anchor-1").map(
        (block) => block.element_key,
      ),
    ).toEqual(["chart-a", "figure-a"]);
    expect(
      visualElementRefs(
        visualElementsSharingAnchor(snapshotWith(blocks), "anchor-1"),
      ),
    ).toEqual([
      { kind: "chart", key: "chart-a" },
      { kind: "figure", key: "figure-a" },
    ]);
    expect(visualElementsSharingAnchor(snapshotWith(blocks), "other")).toEqual(
      [],
    );
    expect(visualElementsSharingAnchor(snapshotWith(blocks), null)).toEqual([]);
  });

  it("truncates anchor excerpts without cutting words silently", () => {
    expect(
      paragraphExcerpt(
        visualBlock({
          block_id: "para",
          kind: "paragraph",
          text: "A short anchor paragraph.",
        }),
      ),
    ).toBe("A short anchor paragraph.");
    expect(
      paragraphExcerpt(
        visualBlock({
          block_id: "para",
          kind: "paragraph",
          text: "x".repeat(100),
        }),
        20,
      ),
    ).toHaveLength(20);
  });
});
