import { describe, expect, it } from "vitest";

import {
  EDITORIAL_REFUSAL_FALLBACK,
  editorialRefusalMessage,
} from "./editorialRefusalCopy";

describe("editorialRefusalMessage", () => {
  it("keeps the two no-proof codes distinct", () => {
    const insufficient = editorialRefusalMessage("bar_chart_insufficient_data");
    const notEvidenced = editorialRefusalMessage(
      "bar_chart_value_not_evidenced",
    );
    expect(insufficient).not.toBe(notEvidenced);
    expect(insufficient).toContain("pas assez de données quantitatives");
    expect(notEvidenced).toContain("n’est pas étayée");
  });

  it("maps every flagged closed code to precise French copy", () => {
    const codes = [
      "editorial_figure_not_eligible",
      "editorial_figure_not_changed",
      "brief_figure_limit_exceeded",
      "editorial_caption_text_violation",
      "enrichment_revision_patch_invalid",
      "enrichment_element_not_found",
      "enrichment_element_kind_mismatch",
      "enrichment_target_stale",
      "enrichment_placement_changed",
      "enrichment_caption_invalid",
      "enrichment_move_anchor_invalid",
      "enrichment_order_invalid",
      "enrichment_order_group_unsupported",
      "enrichment_revision_no_change",
      "enrichment_revision_unsupported_operation",
    ];
    for (const code of codes) {
      const message = editorialRefusalMessage(code);
      expect(message).not.toBe(EDITORIAL_REFUSAL_FALLBACK);
      expect(message).not.toContain(code);
    }
  });

  it("falls back for unknown, malformed or missing codes", () => {
    expect(editorialRefusalMessage("unknown_code")).toBe(
      EDITORIAL_REFUSAL_FALLBACK,
    );
    expect(editorialRefusalMessage(undefined)).toBe(EDITORIAL_REFUSAL_FALLBACK);
    expect(editorialRefusalMessage(42, "Fallback précis")).toBe(
      "Fallback précis",
    );
  });
});
