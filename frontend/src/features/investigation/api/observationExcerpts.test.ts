import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getObservationExcerpt,
  observationExcerptKeys,
} from "./observationExcerpts";
import { installFetch, jsonResponse, lastRequest } from "./testSupport";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("canonical observation excerpts", () => {
  it("reads a bounded excerpt with the requested view and range", async () => {
    const recorder = installFetch(() =>
      jsonResponse({
        view: "text",
        offset: 416,
        length: 24,
        total_size: 8192,
        blob_sha256: "a".repeat(64),
        excerpt_sha256: "b".repeat(64),
        untrusted_data: true,
        text: "print('example')",
      }),
    );

    await getObservationExcerpt("inv-1", "obs/with space", {
      offset: 416,
      length: 24,
      view: "text",
    });

    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/observations/obs%2Fwith%20space/excerpt?offset=416&length=24&view=text",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");
  });

  it("scopes query keys to the observation, exact range, and view", () => {
    expect(
      observationExcerptKeys.detail("inv-1", "obs-1", {
        offset: 416,
        length: 24,
        view: "hex",
      }),
    ).toEqual([
      "investigation",
      "inv-1",
      "observation-excerpts",
      "obs-1",
      416,
      24,
      "hex",
    ]);
  });
});
