import type { EditorialWorkspaceBlock } from "../../api/editorialWorkspace";

export type EditorialManualTextOperation =
  "rewrite_title" | "rewrite_lead" | "rewrite_paragraph";

type ManualEditBlock = Pick<
  EditorialWorkspaceBlock,
  "kind" | "text" | "allowed_operations"
>;

/**
 * The single authority mapping a snapshot block to the canonical manual-edit
 * operation. Keep it in one place: the save path and every enable/disable
 * decision must agree, otherwise a control can be shown but the save fails.
 */
export function manualEditOperationFor(
  block: ManualEditBlock,
): EditorialManualTextOperation | null {
  if (block.text === null) return null;
  switch (block.kind) {
    case "title":
      return block.allowed_operations.includes("rewrite_title")
        ? "rewrite_title"
        : null;
    case "lead_paragraph":
      return block.allowed_operations.includes("rewrite_lead")
        ? "rewrite_lead"
        : null;
    case "paragraph":
      return block.allowed_operations.includes("rewrite_paragraph")
        ? "rewrite_paragraph"
        : null;
    default:
      return null;
  }
}

export function manualEditUnavailableReason({
  block,
  readOnly,
  manualEditEnabled,
}: {
  block: EditorialWorkspaceBlock | null;
  readOnly: boolean;
  manualEditEnabled: boolean;
}): string | null {
  if (!block) return "Sélectionnez un bloc pour le modifier.";
  if (readOnly) {
    return "L’atelier est en lecture seule : aucune modification canonique n’est possible.";
  }
  if (!manualEditEnabled) {
    return "La modification manuelle n’est pas activée pour cet atelier.";
  }
  if (manualEditOperationFor(block)) return null;
  if (block.text === null)
    return "Ce bloc ne contient pas de texte modifiable.";
  if (block.kind === "section_heading") {
    return "La modification directe d’un intertitre n’est pas disponible : aucune opération canonique dédiée n’est exposée par cet atelier.";
  }
  return "Ce bloc ne peut pas être modifié directement dans cet atelier.";
}
