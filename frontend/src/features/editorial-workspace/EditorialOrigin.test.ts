import { describe, expect, it } from "vitest";

import { editorialPortForWorktree } from "../../../e2e/editorial/editorialOrigin";

describe("editorial Playwright worktree ports", () => {
  it("assigns different ports to different worktree paths", () => {
    const first = editorialPortForWorktree(
      "/worktrees/m22-t-m1-05-first",
      null,
    );
    const second = editorialPortForWorktree(
      "/worktrees/m22-t-m1-05-second",
      null,
    );

    expect(first).not.toBe(second);
    expect(first).toBeGreaterThanOrEqual(40_000);
    expect(second).toBeLessThanOrEqual(59_999);
  });

  it("uses a validated explicit port when one is provided", () => {
    expect(editorialPortForWorktree("/worktrees/test", "43123")).toBe(43123);
    expect(() => editorialPortForWorktree("/worktrees/test", "0")).toThrow(
      "EDITORIAL_E2E_PORT must be an integer between 1024 and 65535",
    );
  });
});
