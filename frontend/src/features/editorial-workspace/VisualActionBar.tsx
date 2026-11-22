import type { EditorialWorkspaceBlock } from "../../api/editorialWorkspace";
import {
  canEditVisualCaption,
  canMoveVisualElement,
  canRemoveVisualElement,
  canReorderVisualElements,
  canReplaceVisualElement,
  canRequestVisualBridgeCorrection,
  visualElementTitle,
} from "./visualElementOperations";

interface VisualActionBarProps {
  block: EditorialWorkspaceBlock;
  readOnly: boolean;
  canDiscuss: boolean;
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
 * to an operation the server snapshot actually allows; unavailable actions are
 * absent rather than dead.
 */
export function VisualActionBar({
  block,
  readOnly,
  canDiscuss,
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
  const canCaption = !readOnly && canEditVisualCaption(block) && onEditCaption;
  const canBridge =
    !readOnly &&
    canDiscuss &&
    canRequestVisualBridgeCorrection(block) &&
    onAskBridge;
  const canMove = !readOnly && canMoveVisualElement(block) && onStartMove;
  const canReorder =
    !readOnly && canReorderVisualElements(block) && onStartReorder;
  const canReplace =
    !readOnly && canReplaceVisualElement(block) && onRequestReplace;
  const canRemove =
    !readOnly && canRemoveVisualElement(block) && onRequestRemove;
  const canViewSources = block.evidence_refs.length > 0 && onOpenSources;

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
    return null;
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
      {readOnly ? (
        <p className="visual-action-bar__readonly" role="note">
          Lecture seule : les révisions de ce visuel sont indisponibles.
        </p>
      ) : null}
    </div>
  );
}
