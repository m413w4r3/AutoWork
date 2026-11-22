import { createElement, useCallback, useState, type ReactNode } from "react";

import type { EditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";
import { ReplaceFigureDialog } from "./ReplaceFigureDialog";
import {
  resolveReplaceFigureTarget,
  type ReplaceFigureFlow,
  type ReplaceFigureTarget,
} from "./replaceFigureContract";

export interface ReplaceFigureFlowState extends ReplaceFigureFlow {
  /** Dialog node to render once a request has been made. */
  dialog: ReactNode;
  /** Set when the requested element is no longer in the snapshot. */
  unresolvedError: string | null;
}

export interface ReplaceFigureFlowOptions {
  /** Called after the replacement proposal was accepted canonically. */
  onAccepted?: (target: ReplaceFigureTarget) => void;
  /** Offered with stale/conflict refusals; never triggers a silent retry. */
  onRefresh?: () => void;
}

/**
 * Workspace-level host for F3-A: `requestReplace` is the only entry point and
 * the returned `dialog` is rendered once at the workspace root. Without a
 * request no dialog is mounted.
 */
export function useReplaceFigureFlow(
  subjectId: string,
  snapshot: EditorialWorkspaceSnapshot,
  options: ReplaceFigureFlowOptions = {},
): ReplaceFigureFlowState {
  const [target, setTarget] = useState<ReplaceFigureTarget | null>(null);
  const [unresolvedError, setUnresolvedError] = useState<string | null>(null);

  const requestReplace = useCallback(
    (elementKey: string) => {
      const resolved = resolveReplaceFigureTarget(snapshot, elementKey);
      if (!resolved) {
        setTarget(null);
        setUnresolvedError(
          "Cette figure n’est plus présente dans cette version du document. Actualisez l’atelier.",
        );
        return;
      }
      setUnresolvedError(null);
      setTarget(resolved);
    },
    [snapshot],
  );

  const { onAccepted, onRefresh } = options;
  const dialog = target
    ? createElement(ReplaceFigureDialog, {
        subjectId,
        snapshot,
        target,
        onClose: () => setTarget(null),
        onAccepted: () => onAccepted?.(target),
        onRefresh,
      })
    : null;

  return { requestReplace, dialog, unresolvedError };
}
