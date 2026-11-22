import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditorialProposalAcceptResult,
  EditorialVisualRevisionProposalRead,
} from "../../api/editorialWorkspace";
import { useManualVisualRevision } from "./useManualVisualRevision";
import {
  ANCHOR_TWO_ID,
  CHART_BLOCK_ID,
  VISUAL_SNAPSHOT_ID,
  VISUAL_SUBJECT_ID,
} from "./visualTestFixtures";

const proposal: EditorialVisualRevisionProposalRead = {
  id: "11111111-1111-4111-8111-111111111111",
  subject_id: VISUAL_SUBJECT_ID,
  production_run_id: "run-visual",
  occurrence_id: CHART_BLOCK_ID,
  thread_id: null,
  origin: "manual_patch",
  state: "proposed",
  state_version: 1,
  operation: "move_enrichment_element",
  base_artifact_id: "artifact-visual",
  base_artifact_version: 1,
  base_canonical_sha256: "a".repeat(64),
  base_text: null,
  patch_sha256: "b".repeat(64),
  patch: {
    schema_version: "editorial-enrichment-visual-patch-v1",
    operation: "move_enrichment_element",
    target: {},
    target_id: "target",
    occurrence_id: CHART_BLOCK_ID,
    before_hash: "a".repeat(64),
    visual_patch: {
      kind: "move_enrichment_element",
      element_kind: "chart",
      element_key: "chart-a",
      anchor_block_id: ANCHOR_TWO_ID,
    },
    rationale: "Révision visuelle manuelle.",
  },
  diagnostics: {},
  created_at: "2026-11-22T10:00:00Z",
};

const acceptance: EditorialProposalAcceptResult = {
  id: proposal.id,
  state: "accepted",
  state_version: 2,
  artifact_id: "22222222-2222-4222-8222-222222222222",
  artifact_version: 2,
  snapshot_id: "33333333-3333-4333-8333-333333333333",
  impact_id: "44444444-4444-4444-8444-444444444444",
  impact_state: "pending",
};

interface RecordedCall {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

const calls: RecordedCall[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installFetch(
  handler: (call: RecordedCall, index: number) => Response,
): void {
  vi.stubGlobal(
    "fetch",
    (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const headers: Record<string, string> = {};
      new Headers(init?.headers).forEach((value, key) => {
        headers[key] = value;
      });
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      const call: RecordedCall = {
        url,
        method: init?.method ?? "GET",
        headers,
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      };
      calls.push(call);
      return Promise.resolve(handler(call, calls.length - 1));
    },
  );
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  calls.length = 0;
  vi.unstubAllGlobals();
});

const revisionInput = {
  occurrenceId: CHART_BLOCK_ID,
  revision: {
    snapshot_id: VISUAL_SNAPSHOT_ID,
    operation: "move_enrichment_element" as const,
    element_kind: "chart" as const,
    element_key: "chart-a",
    anchor_block_id: ANCHOR_TWO_ID,
  },
};

describe("useManualVisualRevision", () => {
  it("prepares the typed revision then adopts the same proposal", async () => {
    installFetch((call) =>
      call.url.endsWith("/accept")
        ? jsonResponse(acceptance)
        : jsonResponse(proposal),
    );
    const { result } = renderHook(
      () => useManualVisualRevision(VISUAL_SUBJECT_ID),
      { wrapper },
    );

    const outcome = await result.current.mutateAsync(revisionInput);

    expect(outcome.proposal.id).toBe(proposal.id);
    expect(outcome.acceptance?.artifact_version).toBe(2);
    expect(calls.map((call) => call.method)).toEqual(["POST", "POST"]);
    expect(calls[0]?.url).toBe(
      `/api/subjects/${VISUAL_SUBJECT_ID}/editor/blocks/${CHART_BLOCK_ID}/enrichment-revisions`,
    );
    expect(calls[0]?.headers["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
    expect(calls[0]?.body).toEqual(revisionInput.revision);
    expect(calls[1]?.url).toBe(
      `/api/subjects/${VISUAL_SUBJECT_ID}/editor/threads/${CHART_BLOCK_ID}/proposals/${proposal.id}/accept`,
    );
    expect(calls[1]?.body).toEqual({
      expected_state_version: 1,
      patch_sha256: "b".repeat(64),
    });
  });

  it("treats an already accepted proposal as success after a lost accept response", async () => {
    installFetch(() => jsonResponse({ ...proposal, state: "accepted" }));
    const { result } = renderHook(
      () => useManualVisualRevision(VISUAL_SUBJECT_ID),
      { wrapper },
    );

    const outcome = await result.current.mutateAsync(revisionInput);

    expect(outcome.proposal.state).toBe("accepted");
    expect(outcome.acceptance).toBeNull();
    expect(calls.map((call) => call.method)).toEqual(["POST"]);
    expect(calls[0]?.url).toContain("/enrichment-revisions");
  });

  it("reconciles a state conflict by re-reading an accepted proposal", async () => {
    installFetch((call) => {
      if (call.url.endsWith("/accept")) {
        return jsonResponse(
          {
            detail: {
              code: "proposal_state_conflict",
              message:
                "La proposition a changé. Actualisez-la avant de décider.",
            },
          },
          409,
        );
      }
      if (call.method === "GET" && call.url.includes("/proposals/")) {
        return jsonResponse({
          ...proposal,
          state: "accepted",
          state_version: 2,
        });
      }
      return jsonResponse(proposal);
    });
    const { result } = renderHook(
      () => useManualVisualRevision(VISUAL_SUBJECT_ID),
      { wrapper },
    );

    const outcome = await result.current.mutateAsync(revisionInput);

    expect(outcome.proposal.state).toBe("accepted");
    expect(outcome.acceptance).toBeNull();
    expect(calls.map((call) => call.method)).toEqual(["POST", "POST", "GET"]);
    // The accept decision keeps a stable idempotency key.
    const acceptCalls = calls.filter((call) => call.url.endsWith("/accept"));
    expect(acceptCalls).toHaveLength(1);
    expect(acceptCalls[0]?.headers["idempotency-key"]).toMatch(
      /^[0-9a-f-]{36}$/,
    );
  });

  it("reuses the same prepare and accept keys after an acceptance failure", async () => {
    installFetch((call) => {
      if (call.url.endsWith("/accept")) {
        return jsonResponse(
          { detail: { code: "proposal_base_stale", message: "Conflit." } },
          409,
        );
      }
      return jsonResponse(proposal);
    });
    const { result } = renderHook(
      () => useManualVisualRevision(VISUAL_SUBJECT_ID),
      { wrapper },
    );

    await expect(result.current.mutateAsync(revisionInput)).rejects.toThrow();
    await waitFor(() => expect(result.current.isError).toBe(true));
    await expect(result.current.mutateAsync(revisionInput)).rejects.toThrow();

    const prepareCalls = calls.filter((call) =>
      call.url.endsWith("/enrichment-revisions"),
    );
    expect(prepareCalls).toHaveLength(2);
    expect(prepareCalls[0]?.headers["idempotency-key"]).toBe(
      prepareCalls[1]?.headers["idempotency-key"],
    );
    const acceptCalls = calls.filter((call) => call.url.endsWith("/accept"));
    expect(acceptCalls).toHaveLength(2);
    expect(acceptCalls[0]?.headers["idempotency-key"]).toBe(
      acceptCalls[1]?.headers["idempotency-key"],
    );
  });
});
