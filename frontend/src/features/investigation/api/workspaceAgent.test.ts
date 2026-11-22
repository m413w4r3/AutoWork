import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getWorkspaceAgentStatus,
  useWorkspaceAgentStatus,
  workspaceAgentKeys,
  type WorkspaceAgentStatusResponse,
} from "./workspaceAgent";
import {
  createTestQueryClient,
  installFetch,
  jsonResponse,
  lastRequest,
  wrapperFor,
} from "./testSupport";

afterEach(() => {
  vi.unstubAllGlobals();
});

const available: WorkspaceAgentStatusResponse = {
  status: "available",
  instance_id: "agent-7",
};

describe("workspace agent status", () => {
  it("GETs /api/workspace-agent/status", async () => {
    const recorder = installFetch(() => jsonResponse(available));

    await expect(getWorkspaceAgentStatus()).resolves.toEqual(available);

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/workspace-agent/status");
    expect(request.init?.method).toBe("GET");
  });

  it("parses a typed refusal", async () => {
    installFetch(() =>
      jsonResponse({ detail: { code: "agent_sessions_unavailable" } }, 503),
    );

    await expect(getWorkspaceAgentStatus()).rejects.toMatchObject({
      status: 503,
      code: "agent_sessions_unavailable",
    });
  });

  it("keeps a stable query key", () => {
    expect(workspaceAgentKeys.status()).toEqual(["workspace-agent", "status"]);
    expect(workspaceAgentKeys.all()).toEqual(["workspace-agent"]);
  });

  it("fetches through the TanStack Query hook", async () => {
    installFetch(() => jsonResponse(available));
    const client = createTestQueryClient();

    const { result } = renderHook(() => useWorkspaceAgentStatus(), {
      wrapper: wrapperFor(client),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(available);
  });
});
