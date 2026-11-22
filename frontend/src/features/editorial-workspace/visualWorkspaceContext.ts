import { createContext, useContext } from "react";

import type {
  EditorialEnrichmentElementKind,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { paragraphExcerpt } from "./visualElementOperations";

/** Workspace-owned visual actions exposed to the inspector `Visuels` tab. */
export interface VisualWorkspaceActions {
  /** Select the visual block in the document. */
  selectBlock: (blockId: string) => void;
  /** Route a replacement request through the workspace replace host. */
  requestReplace: (elementKey: string) => void;
  /** Open the shared removal confirmation for one visual element. */
  requestRemove: (elementKey: string) => void;
  /** Start the inline caption revision for one visual element. */
  requestCaption: (elementKey: string) => void;
  /** Enter the shared move mode for one visual element. */
  requestMove: (elementKey: string) => void;
}

/** Active in-document move draft, mirrored by the inspector move panel. */
export interface VisualMoveState {
  elementBlockId: string;
  draftAnchorId: string | null;
  saving: boolean;
  error: string | null;
}

export interface VisualWorkspaceContextValue {
  snapshot: EditorialWorkspaceSnapshot;
  readOnly: boolean;
  /** Re-select the element with this identity after the snapshot refreshes. */
  restoreElementSelection: (
    kind: EditorialEnrichmentElementKind,
    key: string,
    previousBlockId?: string,
  ) => void;
  /** Readable excerpt of one paragraph anchor, for proposal comparisons. */
  anchorLabel: (blockId: string) => string | null;
  /** Single action bus reused by the document action bar and the list. */
  visualActions: VisualWorkspaceActions;
  /** Non-null while one visual is being moved. */
  visualMove: VisualMoveState | null;
  chooseMoveAnchor: (anchorBlockId: string) => void;
  confirmMove: () => void;
  cancelMove: () => void;
}

export const VisualWorkspaceContext =
  createContext<VisualWorkspaceContextValue | null>(null);

/**
 * Optional workspace bridge for components rendered inside the inspector,
 * whose owner lives in another task. Components must work when the provider is
 * absent (unit tests, legacy routes).
 */
export function useVisualWorkspaceContext(): VisualWorkspaceContextValue | null {
  return useContext(VisualWorkspaceContext);
}

export function anchorLabelFromSnapshot(
  snapshot: EditorialWorkspaceSnapshot,
  blockId: string,
): string | null {
  const block = snapshot.blocks.find((item) => item.block_id === blockId);
  return block ? paragraphExcerpt(block, 70) : null;
}
