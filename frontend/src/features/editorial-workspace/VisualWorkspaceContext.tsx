import type { ReactNode } from "react";

import {
  VisualWorkspaceContext,
  type VisualWorkspaceContextValue,
} from "./visualWorkspaceContext";

export function VisualWorkspaceProvider({
  value,
  children,
}: {
  value: VisualWorkspaceContextValue;
  children: ReactNode;
}) {
  return (
    <VisualWorkspaceContext.Provider value={value}>
      {children}
    </VisualWorkspaceContext.Provider>
  );
}
