import { afterEach, describe, expect, it, vi } from "vitest";

import { articleAnchorsKeys, getArticleAnchors } from "./articleAnchors";
import { installFetch, jsonResponse, lastRequest } from "./testSupport";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("article anchor catalog", () => {
  it("reads the canonical anchor route with a plain GET", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ anchors: [], elements: [], truncated: false }),
    );

    await getArticleAnchors("inv-1");

    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/article-anchors",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");
  });

  it("keeps a stable query key scoped to the investigation", () => {
    expect(articleAnchorsKeys.all("inv-1")).toEqual([
      "investigation",
      "inv-1",
      "article-anchors",
    ]);
  });
});
