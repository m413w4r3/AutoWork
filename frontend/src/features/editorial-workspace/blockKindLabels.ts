import type { EditorialWorkspaceBlock } from "../../api/editorialWorkspace";

type BlockKind = EditorialWorkspaceBlock["kind"];

export const BLOCK_KIND_LABELS: Record<BlockKind, string> = {
  title: "Titre",
  lead_paragraph: "Chapeau",
  section_heading: "Intertitre",
  paragraph: "Paragraphe",
  table: "Tableau",
  diagram: "Diagramme",
  chart: "Graphique",
  figure: "Figure",
};

export const OUTLINE_BLOCK_KIND_LABELS: Record<BlockKind, string> = {
  ...BLOCK_KIND_LABELS,
  section_heading: "Section",
};
