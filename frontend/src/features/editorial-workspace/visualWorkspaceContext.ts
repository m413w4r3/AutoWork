import { createContext, useContext } from "react";

import type {
  EditorialEnrichmentElementKind,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { paragraphExcerpt } from "./visualElementOperations";

export interface VisualReplaceRequestState {
  elementKey: string;
  blockId: string;
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
  /** Hand a figure replacement request to the gallery owner. */
  requestReplaceElement: (elementKey: string, blockId: string) => void;
  /** Pending replacement request, consumed by the gallery slot. */
  replaceRequest: VisualReplaceRequestState | null;
  /** Readable excerpt of one paragraph anchor, for proposal comparisons. */
  anchorLabel: (blockId: string) => string | null;
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
