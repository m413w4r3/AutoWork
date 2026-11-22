import type { EditorialWorkspaceBlock } from "../../api/editorialWorkspace";

const MANUAL_TEXT_OPERATIONS = [
  "rewrite_title",
  "rewrite_lead",
  "rewrite_paragraph",
] as const;

export type EditorialManualTextOperation =
  (typeof MANUAL_TEXT_OPERATIONS)[number];

type ManualEditBlock = Pick<
  EditorialWorkspaceBlock,
  "text" | "allowed_operations"
>;

function isManualTextOperation(
  value: string,
): value is EditorialManualTextOperation {
  return (MANUAL_TEXT_OPERATIONS as readonly string[]).includes(value);
}

/**
 * The single authority mapping a snapshot block to the canonical manual-edit
 * operation. The server advertises the accepted operation per block in
 * `allowed_operations`; never guess it from the kind (a lead paragraph is a
 * `rewrite_paragraph` target, not the whole-chapeau `rewrite_lead`). Keep this
 * in one place: the save path and every enable/disable decision must agree,
 * otherwise a control is shown but the save fails.
 */
export function manualEditOperationFor(
  block: ManualEditBlock,
): EditorialManualTextOperation | null {
  if (block.text === null) return null;
  return block.allowed_operations.find(isManualTextOperation) ?? null;
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
