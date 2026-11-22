import { afterEach, describe, expect, it, vi } from "vitest";

import {
  contributionsKeys,
  getContribution,
  importContribution,
  listContributions,
} from "./contributions";
import {
  bodyOf,
  errorResponse,
  headersOf,
  installFetch,
  jsonResponse,
  lastRequest,
} from "./testSupport";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("contribution reads", () => {
  it("lists contributions with every supported filter", async () => {
    const recorder = installFetch(() => jsonResponse([]));

    await listContributions("inv-1", {
      category: "observation",
      import_status: "imported",
      workspace_id: "ws-0123456789abcdef0123456789abcdef",
    });

    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/contributions" +
        "?category=observation&import_status=imported&workspace_id=ws-0123456789abcdef0123456789abcdef",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");
  });

  it("reads one contribution", async () => {
    const recorder = installFetch(() => jsonResponse({}));

    await getContribution("inv-1", "contribution-2");

    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/contributions/contribution-2",
    );
  });
});

describe("contribution import", () => {
  it("imports an observation declaration with Idempotency-Key", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ import_status: "imported" }),
    );

    await importContribution(
      "inv-1",
      {
        category: "observation",
        workspace_id: "ws-0123456789abcdef0123456789abcdef",
        workspace_revision_id: "rev-0123456789abcdef0123456789abcdef",
        origin: "human",
        author_actor_id: "human:analyst",
        observation: {
          object_type: "ip",
          method: "dns_query",
          object_id: "203.0.113.7",
          tool: "dig",
          tool_version: "9.18",
          parameters: { record: "A" },
          limitations: ["synthétique"],
        },
      },
      "idem-contribution-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe("/api/investigations/inv-1/contributions");
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-contribution-1",
    );
    expect(bodyOf(request.init)).toMatchObject({
      category: "observation",
      origin: "human",
      observation: {
        object_type: "ip",
        method: "dns_query",
        object_id: "203.0.113.7",
      },
    });
  });

  it("parses a typed missing-contribution refusal", async () => {
    installFetch(() => errorResponse(404, "work_contribution_not_found"));

    await expect(
      getContribution("inv-1", "contribution-404"),
    ).rejects.toMatchObject({
      status: 404,
      code: "work_contribution_not_found",
    });
  });
});

describe("contribution query keys", () => {
  it("keeps stable keys including every filter", () => {
    expect(contributionsKeys.all("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "contributions",
    ]);
    expect(
      contributionsKeys.list("inv-1", {
        category: "finding",
        import_status: "rejected",
        workspace_id: "ws-1",
      }),
    ).toEqual([
      "investigation",
      "inv-1",
      "contributions",
      "list",
      "finding",
      "rejected",
      "ws-1",
    ]);
    expect(contributionsKeys.detail("inv-1", "contribution-2")).toEqual([
      "investigation",
      "inv-1",
      "contributions",
      "contribution-2",
    ]);
  });
});
