import type { EditorialWorkspaceBlock } from "../../api/editorialWorkspace";

/**
 * Operations the workspace exposes when a manual insertion can really be
 * prepared against the pinned artifacts. The backend never emits a generic
 * `insert_after` operation; it lists the concrete creation routes instead.
 */
export const MANUAL_INSERTION_OPERATIONS: readonly string[] = [
  "create_table",
  "create_bar_chart",
  "create_source_figure",
];

/**
 * Operations consumed by the insertion composer. `insert_after` is kept for
 * compatibility with older snapshots that used it as the placement marker.
 */
export const INSERTION_OPERATIONS: readonly string[] = [
  "insert_after",
  ...MANUAL_INSERTION_OPERATIONS,
];

export function isInsertionAnchor(block: EditorialWorkspaceBlock): boolean {
  return block.kind === "paragraph" || block.kind === "lead_paragraph";
}

export function manualInsertionOperationsFor(
  block: EditorialWorkspaceBlock,
): readonly string[] {
  if (!isInsertionAnchor(block)) return [];
  return MANUAL_INSERTION_OPERATIONS.filter((operation) =>
    block.allowed_operations.includes(operation),
  );
}

export function supportsInsertionAfter(
  block: EditorialWorkspaceBlock,
): boolean {
  if (!isInsertionAnchor(block)) return false;
  return (
    block.allowed_operations.includes("insert_after") ||
    manualInsertionOperationsFor(block).length > 0
  );
}
