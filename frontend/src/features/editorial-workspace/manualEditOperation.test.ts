import { describe, expect, it } from "vitest";

import type { EditorialWorkspaceBlock } from "../../api/editorialWorkspace";
import {
  manualEditOperationFor,
  manualEditUnavailableReason,
} from "./manualEditOperation";

function block(
  kind: EditorialWorkspaceBlock["kind"],
  allowedOperations: string[],
  text: string | null = "Synthetic text",
): EditorialWorkspaceBlock {
  return {
    block_id: `block-${kind}`,
    kind,
    parent_block_id: null,
    position: 0,
    text,
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: `block-${kind}`, legacy_target_id: null },
    before_hash: "a".repeat(64),
    allowed_operations: allowedOperations,
    revision_status: "ready",
  };
}

describe("manualEditOperationFor", () => {
  it("maps the title to the canonical rewrite_title operation", () => {
    expect(manualEditOperationFor(block("title", ["rewrite_title"]))).toBe(
      "rewrite_title",
    );
  });

  it("maps the lead paragraph to rewrite_lead, not rewrite_paragraph", () => {
    expect(
      manualEditOperationFor(
        block("lead_paragraph", ["rewrite_lead", "insert_paragraph"]),
      ),
    ).toBe("rewrite_lead");
  });

  it("maps a paragraph to rewrite_paragraph", () => {
    expect(
      manualEditOperationFor(block("paragraph", ["rewrite_paragraph"])),
    ).toBe("rewrite_paragraph");
  });

  it("never fabricates a manual operation for a section heading", () => {
    expect(
      manualEditOperationFor(
        block("section_heading", [
          "rewrite_section",
          "rename_section",
          "delete_section",
        ]),
      ),
    ).toBeNull();
  });

  it("returns null when the server does not allow the exact operation", () => {
    expect(manualEditOperationFor(block("title", []))).toBeNull();
    expect(manualEditOperationFor(block("lead_paragraph", []))).toBeNull();
    expect(
      manualEditOperationFor(block("paragraph", ["rewrite_lead"])),
    ).toBeNull();
  });

  it("returns null for a block without text", () => {
    expect(
      manualEditOperationFor(block("paragraph", ["rewrite_paragraph"], null)),
    ).toBeNull();
  });
});

describe("manualEditUnavailableReason", () => {
  it("explains the read-only state", () => {
    expect(
      manualEditUnavailableReason({
        block: block("paragraph", ["rewrite_paragraph"]),
        readOnly: true,
        manualEditEnabled: true,
      }),
    ).toContain("lecture seule");
  });

  it("explains a disabled manual-edit capability", () => {
    expect(
      manualEditUnavailableReason({
        block: block("paragraph", ["rewrite_paragraph"]),
        readOnly: false,
        manualEditEnabled: false,
      }),
    ).toContain("n’est pas activée");
  });

  it("gives a precise reason for a section heading instead of a dead button", () => {
    const reason = manualEditUnavailableReason({
      block: block("section_heading", ["rewrite_section"]),
      readOnly: false,
      manualEditEnabled: true,
    });
    expect(reason).toContain("intertitre");
    expect(reason).toContain("opération canonique");
  });

  it("returns null when the block is directly editable", () => {
    expect(
      manualEditUnavailableReason({
        block: block("paragraph", ["rewrite_paragraph"]),
        readOnly: false,
        manualEditEnabled: true,
      }),
    ).toBeNull();
  });
});
