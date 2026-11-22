import { ApiError } from "../../api/editions";
import type {
  EditorialFigureCatalogItem,
  EditorialWorkspaceBlock,
  EditorialWorkspaceFigure,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";

/**
 * Typed contract between F3-A (document block actions) and F3-B (gallery and
 * replacement flow).
 *
 * F3-A renders a `Remplacer` action only when the snapshot declares
 * `replace_source_figure` for the figure element and calls
 * `onRequestReplace(elementKey)`. `elementKey` is the opaque enrichment
 * element key from the snapshot (`EditorialWorkspaceBlock.element_key`); it is
 * never a URL, path or catalog id.
 *
 * Two integration options are exported:
 * - `useReplaceFigureFlow(subjectId, snapshot)` returns `requestReplace` plus
 *   the dialog node to render; this is the smallest wiring for F3-A;
 * - `ReplaceFigureDialog` can be mounted directly when the caller already owns
 *   the request state.
 */

export const BRIEF_FIGURE_LIMIT = 3;

export interface ReplaceFigureTarget {
  /** Opaque enrichment element key declared by the pinned snapshot. */
  elementKey: string;
  /** Occurrence id of the figure block; also the accept route occurrence. */
  blockId: string;
  currentFigure: EditorialWorkspaceFigure;
  /** Anchor paragraph excerpt, when the placement can be resolved. */
  anchorText: string | null;
}

/** Imperative API exposed to F3-A. */
export interface ReplaceFigureFlow {
  requestReplace(elementKey: string): void;
}

/** Callbacks F3-A may provide to the inspector `Visuels` tab. */
export interface VisualActionCallbacks {
  /** Select the visual block in the document (F3-A document selection). */
  onSelectBlock?: (blockId: string) => void;
  /** Route the replacement request through the workspace-level host. */
  onRequestReplace?: (elementKey: string) => void;
  /** Delegate removal to F3-A when it exposes the operation. */
  onRequestRemove?: (elementKey: string) => void;
  /** Delegate caption revision to F3-A when it exposes the operation. */
  onRequestCaption?: (elementKey: string) => void;
  /** Delegate placement change to F3-A when it exposes the operation. */
  onRequestMove?: (elementKey: string) => void;
}

export interface ReplaceFigureDialogProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  target: ReplaceFigureTarget;
  onClose: () => void;
  /** Called after the replacement proposal was accepted canonically. */
  onAccepted?: (elementKey: string) => void;
  /** Offered with stale/conflict refusals; never triggers a silent retry. */
  onRefresh?: () => void;
}

export interface FigureSelectionDraft {
  item: EditorialFigureCatalogItem;
  caption: string;
}

/** Compact French counter used by the gallery and the selection panel. */
export function figureSelectionLabel(count: number): string {
  return count === 1 ? "1 image sélectionnée" : `${count} images sélectionnées`;
}

/** Source label without exposing source UUIDs in the editorial UI. */
export function figureSourceLabel(item: EditorialFigureCatalogItem): string {
  return item.source.title?.trim() || "Source archivée";
}

/** The caption proposed by default when an archived figure is integrated. */
export function defaultFigureCaption(item: EditorialFigureCatalogItem): string {
  return item.original_caption?.trim() || item.original_name;
}

/**
 * Explanation shown on a figure that cannot be selected. The server may send
 * an explicit exclusion reason; otherwise the closed status supplies it.
 */
export function figureExclusionReason(
  item: EditorialFigureCatalogItem,
): string | null {
  if (item.exclusion_reason?.trim()) return item.exclusion_reason.trim();
  if (!item.selectable) {
    switch (item.status) {
      case "already_integrated":
        return "Cette figure est déjà intégrée au document.";
      case "ineligible":
        return "Cette figure n’est pas éligible à l’intégration.";
      case "file_unavailable":
        return "Le fichier archivé de cette figure est indisponible.";
      case "unverified":
        return "Cette figure n’a pas encore été vérifiée.";
      case "not_selected":
        return "Cette figure n’a pas été retenue dans l’inventaire.";
      case "available":
        return "Cette figure ne peut pas être sélectionnée.";
    }
  }
  return null;
}

export function integratedFigureCount(
  snapshot: EditorialWorkspaceSnapshot,
): number {
  return snapshot.blocks.filter(
    (block) => block.kind === "figure" && block.figure?.status === "included",
  ).length;
}

/** Remaining BRIEF capacity, or `null` when the publication has no cap. */
export function briefFigureCapacity(
  snapshot: EditorialWorkspaceSnapshot,
): number | null {
  if (snapshot.publication_kind !== "brief") return null;
  return Math.max(0, BRIEF_FIGURE_LIMIT - integratedFigureCount(snapshot));
}

function visualBlocks(
  snapshot: EditorialWorkspaceSnapshot,
): EditorialWorkspaceBlock[] {
  const byBlockId = new Map<string, EditorialWorkspaceBlock>();
  for (const block of [...snapshot.blocks, ...snapshot.enrichments]) {
    if (!byBlockId.has(block.block_id)) byBlockId.set(block.block_id, block);
  }
  return [...byBlockId.values()].sort(
    (left, right) => left.position - right.position,
  );
}

/**
 * Resolve the integrated figure element targeted by `elementKey`. Returns
 * `null` when the element is absent from this snapshot; callers must surface
 * that state instead of guessing another figure.
 */
export function resolveReplaceFigureTarget(
  snapshot: EditorialWorkspaceSnapshot,
  elementKey: string,
): ReplaceFigureTarget | null {
  const block = visualBlocks(snapshot).find(
    (candidate) =>
      candidate.kind === "figure" &&
      candidate.element_key === elementKey &&
      candidate.figure !== null &&
      candidate.figure !== undefined,
  );
  if (!block?.figure) return null;
  const anchorBlockId = block.figure.anchor_block_id;
  const anchorBlock =
    anchorBlockId === null
      ? null
      : snapshot.blocks.find(
          (candidate) => candidate.block_id === anchorBlockId,
        );
  const anchorText = anchorBlock?.text?.trim() || null;
  return {
    elementKey,
    blockId: block.block_id,
    currentFigure: block.figure,
    anchorText,
  };
}

export interface ReplaceFigureErrorInfo {
  code: string;
  message: string;
  /** True when refreshing the workspace is the safe next action. */
  refresh: boolean;
}

const REPLACE_FIGURE_ERROR_MESSAGES: Record<string, string> = {
  editorial_figure_not_eligible:
    "Cette figure ne peut pas être intégrée : elle n’est plus éligible. Choisissez une autre image archivée.",
  editorial_figure_not_found:
    "Cette figure n’est plus disponible dans l’inventaire : elle a été retirée ou révoquée. Choisissez une autre image archivée.",
  editorial_figure_not_changed:
    "Cette image est déjà celle de la figure affichée. Aucune révision n’a été créée.",
  brief_figure_limit_exceeded:
    "Une brève ne peut contenir plus de trois figures intégrées. Aucune image n’a été remplacée.",
  enrichment_element_not_found:
    "Cette figure n’existe plus dans cette version du document. Actualisez l’atelier.",
  enrichment_element_kind_mismatch:
    "Cet élément n’est plus une figure dans cette version. Actualisez l’atelier.",
  enrichment_target_stale:
    "Cette figure a changé depuis l’ouverture de la galerie. Actualisez l’atelier avant de réessayer.",
  enrichment_placement_changed:
    "L’emplacement de cette figure a changé. Actualisez l’atelier avant de réessayer.",
  editorial_anchor_stale:
    "Le paragraphe d’ancrage a changé ou a été supprimé. Actualisez l’atelier, puis choisissez une image.",
  proposal_base_stale:
    "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et recommencez.",
  editorial_before_hash_mismatch:
    "Cette figure a changé. Rechargez l’atelier avant de réessayer.",
  editorial_snapshot_stale: "Le document a changé. Rechargez l’atelier.",
  proposal_state_conflict:
    "La proposition a changé. Actualisez-la avant de décider.",
  proposal_idempotency_conflict:
    "Cette demande correspond à une autre sélection. Actualisez l’atelier avant de continuer.",
  enrichment_caption_invalid:
    "La légende proposée ne respecte pas les règles de l’atelier.",
  editorial_caption_text_violation:
    "La légende contient un format ou un balisage non autorisé.",
  editorial_read_only:
    "L’atelier est en lecture seule : aucune figure ne peut être remplacée.",
  manual_edit_unavailable:
    "La révision visuelle est temporairement indisponible. Actualisez l’atelier avant de réessayer.",
  enrichment_revision_unsupported_operation:
    "Le remplacement d’image n’est pas encore disponible sur ce serveur. Aucune image n’a été modifiée.",
};

const REPLACE_FIGURE_REFRESH_CODES = new Set([
  "editorial_figure_not_found",
  "editorial_figure_not_eligible",
  "enrichment_element_not_found",
  "enrichment_element_kind_mismatch",
  "enrichment_target_stale",
  "enrichment_placement_changed",
  "editorial_anchor_stale",
  "proposal_base_stale",
  "editorial_before_hash_mismatch",
  "editorial_snapshot_stale",
  "proposal_state_conflict",
  "proposal_idempotency_conflict",
]);

/** Closed-code French copy; never renders a bare server code or raw payload. */
export function replaceFigureErrorInfo(error: unknown): ReplaceFigureErrorInfo {
  if (error instanceof ApiError) {
    const code =
      typeof error.code === "string" && error.code
        ? error.code
        : "replace_source_figure_failed";
    return {
      code,
      message:
        REPLACE_FIGURE_ERROR_MESSAGES[code] ??
        "L’image n’a pas pu être remplacée. Vérifiez la sélection, puis réessayez.",
      refresh: REPLACE_FIGURE_REFRESH_CODES.has(code) || error.status === 409,
    };
  }
  return {
    code: "replace_source_figure_failed",
    message:
      "L’image n’a pas pu être remplacée. Vérifiez la connexion, puis réessayez avec la même sélection.",
    refresh: false,
  };
}
