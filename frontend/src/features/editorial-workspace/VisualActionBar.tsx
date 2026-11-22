import type { EditorialWorkspaceBlock } from "../../api/editorialWorkspace";
import {
  visualElementTitle,
  visualMutationUnavailableReason,
  visualOperationAvailability,
  type VisualCapabilityFlags,
} from "./visualElementOperations";

interface VisualActionBarProps {
  block: EditorialWorkspaceBlock;
  capabilities: VisualCapabilityFlags;
  onEditCaption?: (blockId: string) => void;
  onAskBridge?: (blockId: string) => void;
  onStartMove?: (blockId: string) => void;
  onStartReorder?: (blockId: string) => void;
  onRequestReplace?: (blockId: string) => void;
  onRequestRemove?: (blockId: string) => void;
  onOpenSources?: (blockId: string) => void;
  onOpenHistory?: (blockId: string) => void;
}

/**
 * Stable action bar of one selected visual element. Every rendered button maps
 * to an operation the server snapshot allows *and* to a truthful capability:
 * manual mutations need `manual_edit`, Bridge entries need `can_discuss`, and
 * an unwired proposal path never renders a dead control.
 */
export function VisualActionBar({
  block,
  capabilities,
  onEditCaption,
  onAskBridge,
  onStartMove,
  onStartReorder,
  onRequestReplace,
  onRequestRemove,
  onOpenSources,
  onOpenHistory,
}: VisualActionBarProps) {
  const title = visualElementTitle(block);
  const availability = visualOperationAvailability(block, capabilities);
  const canCaption = availability.caption && onEditCaption;
  const canBridge = availability.bridge && onAskBridge;
  const canMove = availability.move && onStartMove;
  const canReorder = availability.reorder && onStartReorder;
  const canReplace = availability.replace && onRequestReplace;
  const canRemove = availability.remove && onRequestRemove;
  const canViewSources = block.evidence_refs.length > 0 && onOpenSources;
  const mutationReason = visualMutationUnavailableReason(capabilities);

  if (
    !canCaption &&
    !canBridge &&
    !canMove &&
    !canReorder &&
    !canReplace &&
    !canRemove &&
    !canViewSources &&
    !onOpenHistory
  ) {
    return mutationReason || capabilities.read_only ? (
      <div
        className="editorial-block-actions editorial-block-actions--block visual-action-bar"
        role="toolbar"
        aria-label={`Actions du visuel : ${title}`}
        data-element-kind={block.element_kind ?? undefined}
        data-element-key={block.element_key ?? undefined}
      >
        <p className="visual-action-bar__readonly" role="note">
          {mutationReason}
        </p>
      </div>
    ) : null;
  }

  return (
    <div
      className="editorial-block-actions editorial-block-actions--block visual-action-bar"
      role="toolbar"
      aria-label={`Actions du visuel : ${title}`}
      data-element-kind={block.element_kind ?? undefined}
      data-element-key={block.element_key ?? undefined}
    >
      {canCaption ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onEditCaption(block.block_id)}
        >
          Modifier la légende
        </button>
      ) : null}
      {canBridge ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onAskBridge(block.block_id)}
        >
          Demander à Bridge
        </button>
      ) : null}
      {canMove ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onStartMove(block.block_id)}
        >
          Déplacer
        </button>
      ) : null}
      {canReorder ? (
        <button
          className="button button--secondary"
          type="button"
          title="Monter ou descendre les visuels de ce paragraphe"
          onClick={() => onStartReorder(block.block_id)}
        >
          Réorganiser
        </button>
      ) : null}
      {canReplace ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onRequestReplace(block.block_id)}
        >
          Remplacer
        </button>
      ) : null}
      {canRemove ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onRequestRemove(block.block_id)}
        >
          Retirer
        </button>
      ) : null}
      {canViewSources ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onOpenSources(block.block_id)}
        >
          Sources
        </button>
      ) : null}
      {onOpenHistory ? (
        <button
          className="button button--secondary"
          type="button"
          onClick={() => onOpenHistory(block.block_id)}
        >
          Historique
        </button>
      ) : null}
      {mutationReason ? (
        <p className="visual-action-bar__readonly" role="note">
          {mutationReason}
        </p>
      ) : null}
    </div>
  );
}
