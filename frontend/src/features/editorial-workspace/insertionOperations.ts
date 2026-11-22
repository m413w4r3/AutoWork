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

export interface InsertionCapabilityFlags {
  read_only: boolean;
  can_add_enrichment: boolean;
}

/**
 * Precise reason when a manual insertion cannot be prepared for this block, or
 * `null` when the insertion controls may be rendered. The truthful
 * `can_add_enrichment` capability is required in addition to the block's
 * `allowed_operations`.
 */
export function insertionUnavailableReason(
  block: EditorialWorkspaceBlock,
  capabilities: InsertionCapabilityFlags,
): string | null {
  if (capabilities.read_only) {
    return "L’atelier est en lecture seule : aucune insertion ne peut être préparée.";
  }
  if (!capabilities.can_add_enrichment) {
    return "La préparation d’insertions n’est pas activée pour cet atelier.";
  }
  if (!isInsertionAnchor(block)) {
    return "Seuls un paragraphe ou le chapeau peuvent recevoir une insertion après eux.";
  }
  if (!supportsInsertionAfter(block)) {
    return "Aucune insertion après ce bloc n’est autorisée par l’instantané serveur.";
  }
  return null;
}

export function canInsertAfter(
  block: EditorialWorkspaceBlock,
  capabilities: InsertionCapabilityFlags,
): boolean {
  return insertionUnavailableReason(block, capabilities) === null;
}
