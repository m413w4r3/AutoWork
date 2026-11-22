import type {
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";

export const VISUAL_SUBJECT_ID = "subject-visual";
export const VISUAL_SNAPSHOT_ID = "snapshot-visual";
export const ANCHOR_ONE_ID = "anchor-one";
export const ANCHOR_TWO_ID = "anchor-two";
export const CHART_BLOCK_ID = "chart-block";
export const FIGURE_A_BLOCK_ID = "figure-a-block";
export const FIGURE_B_BLOCK_ID = "figure-b-block";
export const DIAGRAM_BLOCK_ID = "diagram-block";

const ALL_VISUAL_OPERATIONS = [
  "update_visual_caption",
  "move_enrichment_element",
  "reorder_enrichment_elements",
] as const;

export function visualBlock(
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

export function paragraphBlock(
  blockId: string,
  text: string,
  parentBlockId: string | null = null,
): EditorialWorkspaceBlock {
  return visualBlock({
    block_id: blockId,
    kind: "paragraph",
    parent_block_id: parentBlockId,
    text,
    allowed_operations: ["rewrite_paragraph"],
  });
}

export function chartBlock(
  overrides: Partial<EditorialWorkspaceBlock> = {},
): EditorialWorkspaceBlock {
  return visualBlock({
    block_id: CHART_BLOCK_ID,
    kind: "chart",
    element_kind: "chart",
    element_key: "chart-a",
    order: 0,
    caption: "Légende actuelle du graphique",
    allowed_operations: [
      "rewrite_chart",
      "delete_chart",
      ...ALL_VISUAL_OPERATIONS,
    ],
    chart: {
      key: "chart-a",
      kind: "bar",
      title: "Titre actuel du graphique",
      caption: "Légende actuelle du graphique",
      legend: ["Série A"],
      category_axis_label: "Catégories",
      value_axis_label: "Valeurs",
      unit: "événements",
      points: [
        {
          label: "Catégorie 1",
          value: "3",
          evidence_refs: [
            {
              source_document_id: "source-1",
              kind: "fact",
              evidence_key: "fact-1",
            },
          ],
        },
      ],
      anchor_block_id: ANCHOR_ONE_ID,
    },
    ...overrides,
  });
}

export function figureBlock({
  blockId,
  elementKey,
  caption,
  order,
  anchorBlockId = ANCHOR_ONE_ID,
  originalCaption = "Légende d’origine archivée",
}: {
  blockId: string;
  elementKey: string;
  caption: string;
  order: number;
  anchorBlockId?: string;
  originalCaption?: string;
}): EditorialWorkspaceBlock {
  return visualBlock({
    block_id: blockId,
    kind: "figure",
    element_kind: "figure",
    element_key: elementKey,
    order,
    caption,
    allowed_operations: [
      "replace_source_figure",
      "delete_source_figure",
      ...ALL_VISUAL_OPERATIONS,
    ],
    figure: {
      key: elementKey,
      catalog_figure_id: null,
      original_caption: originalCaption,
      caption,
      source_url: "https://example.invalid/source",
      source: "Source synthétique",
      provenance: "Provenance synthétique",
      sha256: "b".repeat(64),
      mime_type: "image/png",
      byte_size: 128,
      width: 320,
      height: 180,
      status: "included",
      order,
      anchor_block_id: anchorBlockId,
      placement: "after_paragraph",
      page: 1,
      section: "Section synthétique",
    },
  });
}

export function diagramBlock(
  overrides: Partial<EditorialWorkspaceBlock> = {},
): EditorialWorkspaceBlock {
  return visualBlock({
    block_id: DIAGRAM_BLOCK_ID,
    kind: "diagram",
    element_kind: "diagram",
    element_key: "diagram-a",
    order: 2,
    caption: "Légende du diagramme",
    allowed_operations: [
      "rewrite_diagram",
      "delete_diagram",
      ...ALL_VISUAL_OPERATIONS,
    ],
    diagram: {
      key: "diagram-a",
      kind: "network_flow",
      title: "Titre du diagramme",
      caption: "Légende du diagramme",
      direction: "left_to_right",
      profile: null,
      nodes: [
        { node_id: "n1", label: "Nœud 1", role: null },
        { node_id: "n2", label: "Nœud 2", role: null },
      ],
      edges: [{ source_node_id: "n1", target_node_id: "n2", label: null }],
      groups: [],
      anchor_block_id: ANCHOR_ONE_ID,
      placement: "after_paragraph",
    },
    ...overrides,
  });
}

export function visualSnapshot({
  blocks,
  readOnlyReason = null,
  canDiscuss = true,
}: {
  blocks: EditorialWorkspaceBlock[];
  readOnlyReason?: EditorialWorkspaceSnapshot["capabilities"]["read_only_reason"];
  canDiscuss?: boolean;
}): EditorialWorkspaceSnapshot {
  return {
    subject_id: VISUAL_SUBJECT_ID,
    production_run_id: "run-visual",
    publication_kind: "article",
    synthesis_artifact_id: "synthesis-visual",
    synthesis_version: 1,
    synthesis_canonical_sha256: "a".repeat(64),
    enrichment_artifact_id: "enrichment-visual",
    enrichment_version: 1,
    publication_artifact_id: null,
    publication_version: null,
    pipeline_generation: 1,
    blocks,
    enrichments: blocks.filter((block) => block.element_kind !== null),
    document_outline: [],
    capabilities: {
      can_discuss: canDiscuss,
      can_preview_revision: false,
      manual_edit: true,
      can_add_enrichment: false,
      can_preview_pdf: false,
      can_regenerate_pdf: false,
      read_only_reason: readOnlyReason,
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
    snapshot_id: VISUAL_SNAPSHOT_ID,
  };
}
