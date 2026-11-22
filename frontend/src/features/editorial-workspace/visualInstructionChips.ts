import type { EditorialEnrichmentElementKind } from "../../api/editorialWorkspace";

export interface VisualInstructionChip {
  label: string;
  instruction: string;
}

/**
 * Editable instruction chips offered when a visual element opens its Bridge
 * thread. A chip only prefills the composer instruction; it never sends.
 */
const CHART_CHIPS: readonly VisualInstructionChip[] = [
  {
    label: "Corriger le titre",
    instruction: "Corrige le titre pour mieux décrire les données.",
  },
  {
    label: "Regrouper les catégories",
    instruction: "Regroupe les catégories sans modifier les valeurs.",
  },
  {
    label: "Réorganiser les barres",
    instruction: "Réorganise l’ordre des barres sans changer les valeurs.",
  },
  {
    label: "Vérifier l’unité",
    instruction:
      "Vérifie l’unité de mesure et propose une correction étayée par les preuves admises.",
  },
  {
    label: "Simplifier la légende",
    instruction: "Simplifie la légende sans retirer d’information établie.",
  },
];

const DIAGRAM_CHIPS: readonly VisualInstructionChip[] = [
  {
    label: "Corriger les libellés",
    instruction:
      "Corrige les libellés du diagramme sans changer les relations établies.",
  },
  {
    label: "Retirer une relation non prouvée",
    instruction:
      "Retire la relation qui n’est pas étayée par les preuves admises, sans en inventer d’autre.",
  },
  {
    label: "Clarifier la structure",
    instruction:
      "Clarifie la structure du diagramme sans ajouter de nœud ni de relation non étayée.",
  },
];

const CAPTION_CHIPS: readonly VisualInstructionChip[] = [
  {
    label: "Clarifier la légende",
    instruction: "Clarifie la légende sans ajouter de fait non étayé.",
  },
  {
    label: "Raccourcir la légende",
    instruction: "Raccourcis la légende sans perdre d’information établie.",
  },
  {
    label: "Corriger les imprécisions",
    instruction:
      "Corrige les imprécisions de la légende en t’appuyant sur les preuves admises.",
  },
];

export function visualInstructionChips(
  kind: EditorialEnrichmentElementKind,
): readonly VisualInstructionChip[] {
  if (kind === "chart") return CHART_CHIPS;
  if (kind === "diagram") return DIAGRAM_CHIPS;
  return CAPTION_CHIPS;
}
