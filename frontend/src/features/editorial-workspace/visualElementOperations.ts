import type {
  EditorialEnrichmentElementKind,
  EditorialEnrichmentElementRef,
  EditorialVisualRevisionPatch,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { BLOCK_KIND_LABELS } from "./blockKindLabels";

/**
 * A visual element is an enrichment occurrence with a server-declared element
 * identity. Blocks without both fields are not selectable visual elements: the
 * workshop never guesses an identity from the DOM order or from the payload.
 */
export interface VisualElementIdentity {
  kind: EditorialEnrichmentElementKind;
  key: string;
}

const REMOVE_OPERATIONS: Record<EditorialEnrichmentElementKind, string> = {
  table: "delete_table",
  chart: "delete_chart",
  diagram: "delete_diagram",
  figure: "delete_source_figure",
};

/** `allowed_operations` value the server uses to advertise element removal. */
export function visualRemoveOperation(
  kind: EditorialEnrichmentElementKind,
): string {
  return REMOVE_OPERATIONS[kind];
}

/**
 * Operation a Bridge correction thread may propose for this element kind.
 * Move, reorder, replace and removal stay manual-only.
 */
export function visualBridgeOperation(
  kind: EditorialEnrichmentElementKind,
): "rewrite_chart" | "rewrite_diagram" | "update_visual_caption" {
  if (kind === "chart") return "rewrite_chart";
  if (kind === "diagram") return "rewrite_diagram";
  return "update_visual_caption";
}

export function visualElementIdentity(
  block: EditorialWorkspaceBlock,
): VisualElementIdentity | null {
  if (!block.element_kind || !block.element_key) return null;
  return { kind: block.element_kind, key: block.element_key };
}

export function isVisualElementBlock(block: EditorialWorkspaceBlock): boolean {
  return visualElementIdentity(block) !== null;
}

export function visualElementRef(
  block: EditorialWorkspaceBlock,
): EditorialEnrichmentElementRef | null {
  const identity = visualElementIdentity(block);
  return identity ? { kind: identity.kind, key: identity.key } : null;
}

/**
 * Element identity targeted by one persisted visual patch, when it names one.
 * Reorder patches target an anchor group rather than a single element.
 */
export function visualPatchTarget(
  patch: EditorialVisualRevisionPatch,
): VisualElementIdentity | null {
  switch (patch.kind) {
    case "update_visual_caption":
    case "move_enrichment_element":
    case "remove_enrichment_element":
      return { kind: patch.element_kind, key: patch.element_key };
    case "revise_chart":
      return { kind: "chart", key: patch.element_key };
    case "revise_diagram":
      return { kind: "diagram", key: patch.element_key };
    case "replace_source_figure":
      return { kind: "figure", key: patch.element_key };
    default:
      return null;
  }
}

/** Placement identity of an element, read from its typed projection. */
export function visualElementAnchorId(
  block: EditorialWorkspaceBlock,
): string | null {
  if (block.table) return block.table.anchor_block_id;
  if (block.chart) return block.chart.anchor_block_id;
  if (block.diagram) return block.diagram.anchor_block_id;
  if (block.figure) return block.figure.anchor_block_id;
  return null;
}

/** Human title of a visual element, never an internal id or key. */
export function visualElementTitle(block: EditorialWorkspaceBlock): string {
  if (block.chart) return block.chart.title;
  if (block.table) return block.table.title;
  if (block.diagram) return block.diagram.title;
  if (block.figure) return block.figure.caption;
  const text = block.text?.trim();
  return text || BLOCK_KIND_LABELS[block.kind];
}

export function visualElementKindLabel(block: EditorialWorkspaceBlock): string {
  return BLOCK_KIND_LABELS[block.kind];
}

interface OperationSource {
  allowed_operations: readonly string[];
}

export function canEditVisualCaption(block: OperationSource): boolean {
  return block.allowed_operations.includes("update_visual_caption");
}

export function canMoveVisualElement(block: OperationSource): boolean {
  return block.allowed_operations.includes("move_enrichment_element");
}

export function canReorderVisualElements(
  block: OperationSource & {
    element_kind?: EditorialEnrichmentElementKind | null;
  },
): boolean {
  return (
    (block.element_kind ?? null) !== null &&
    block.allowed_operations.includes("reorder_enrichment_elements")
  );
}

export function canReplaceVisualElement(
  block: OperationSource & {
    element_kind?: EditorialEnrichmentElementKind | null;
  },
): boolean {
  return (
    block.element_kind === "figure" &&
    block.allowed_operations.includes("replace_source_figure")
  );
}

export function canRemoveVisualElement(
  block: OperationSource & {
    element_kind?: EditorialEnrichmentElementKind | null;
  },
): boolean {
  const kind = block.element_kind;
  if (!kind) return false;
  return block.allowed_operations.includes(visualRemoveOperation(kind));
}

export function canRequestVisualBridgeCorrection(
  block: OperationSource & {
    element_kind?: EditorialEnrichmentElementKind | null;
  },
): boolean {
  const kind = block.element_kind;
  if (!kind) return false;
  return block.allowed_operations.includes(visualBridgeOperation(kind));
}

function groupRank(block: EditorialWorkspaceBlock): [number, string, string] {
  return [
    block.order ?? Number.MAX_SAFE_INTEGER,
    block.element_kind ?? "",
    block.element_key ?? "",
  ];
}

/**
 * Elements that share one paragraph anchor, in their canonical document order.
 * `snapshot.blocks` is already the server-resolved publication order; `order`
 * is the canonical rank inside the group. Both agree for a well-formed
 * snapshot and the stable sort keeps the document order otherwise.
 */
export function visualElementsSharingAnchor(
  snapshot: Pick<EditorialWorkspaceSnapshot, "blocks">,
  anchorBlockId: string | null,
): EditorialWorkspaceBlock[] {
  if (!anchorBlockId) return [];
  return snapshot.blocks
    .map((block, index) => ({ block, index }))
    .filter(
      ({ block }) =>
        isVisualElementBlock(block) &&
        block.revision_status === "ready" &&
        visualElementAnchorId(block) === anchorBlockId,
    )
    .sort((left, right) => {
      const byRank = groupRank(left.block);
      const byOther = groupRank(right.block);
      for (let index = 0; index < byRank.length; index += 1) {
        const first = byRank[index] ?? "";
        const second = byOther[index] ?? "";
        if (first < second) return -1;
        if (first > second) return 1;
      }
      return left.index - right.index;
    })
    .map(({ block }) => block);
}

export function visualElementRefs(
  blocks: readonly EditorialWorkspaceBlock[],
): EditorialEnrichmentElementRef[] {
  return blocks.flatMap((block) => {
    const ref = visualElementRef(block);
    return ref ? [ref] : [];
  });
}

export function sameElementIdentity(
  block: Pick<EditorialWorkspaceBlock, "element_kind" | "element_key">,
  identity: VisualElementIdentity,
): boolean {
  return (
    block.element_kind === identity.kind && block.element_key === identity.key
  );
}

/** Anchor paragraph excerpt used by the provisional move preview. */
export function paragraphExcerpt(
  block: EditorialWorkspaceBlock,
  maximum = 60,
): string {
  const text = block.text?.trim() ?? "";
  if (text.length <= maximum) return text;
  return `${text.slice(0, maximum - 1).trimEnd()}…`;
}
