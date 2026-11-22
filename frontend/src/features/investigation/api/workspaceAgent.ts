/**
 * Host workspace-agent status (`api/workspace_agent.py`, W6).
 *
 * Read-only probe. An unavailable agent blocks workspace actions with the
 * typed `workspace_local_unavailable` refusal but never fails global
 * readiness, so the cockpit shows this status instead of retrying.
 */

import { useQuery } from "@tanstack/react-query";

import { getJson } from "./client";
import type { CommonErrorCode } from "./types";

export type WorkspaceAgentStatus = "available" | "unavailable" | "unconfigured";

export interface WorkspaceAgentStatusResponse {
  status: WorkspaceAgentStatus;
  instance_id: string | null;
}

export type WorkspaceAgentErrorCode = CommonErrorCode;

export const workspaceAgentKeys = {
  all: () => ["workspace-agent"] as const,
  status: () => ["workspace-agent", "status"] as const,
};

export function getWorkspaceAgentStatus(
  signal?: AbortSignal,
): Promise<WorkspaceAgentStatusResponse> {
  return getJson<WorkspaceAgentStatusResponse>(
    "/api/workspace-agent/status",
    signal,
  );
}

export function useWorkspaceAgentStatus(options: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: workspaceAgentKeys.status(),
    queryFn: ({ signal }) => getWorkspaceAgentStatus(signal),
    enabled: options.enabled ?? true,
  });
}
