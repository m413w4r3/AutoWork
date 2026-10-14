import { describe, expect, it } from "vitest";

import { createApiProxyOptions } from "./viteProxyConfig";

describe("createApiProxyOptions", () => {
  it("preserves the current proxy behavior when no server token is configured", () => {
    expect(createApiProxyOptions("http://localhost:8000", undefined)).toEqual({
      target: "http://localhost:8000",
      changeOrigin: true,
    });
  });

  it("adds a bearer token only to the server-side proxy options", () => {
    expect(
      createApiProxyOptions(
        "http://backend:8000",
        "operator-token-for-tests-123456",
      ),
    ).toEqual({
      target: "http://backend:8000",
      changeOrigin: true,
      headers: { Authorization: "Bearer operator-token-for-tests-123456" },
    });
  });
});
