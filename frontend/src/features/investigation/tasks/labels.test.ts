import { describe, expect, it } from "vitest";

import { actorRefLabel } from "./labels";

describe("actorRefLabel", () => {
  it("traduit un adaptateur opérateur et retire le préfixe de type", () => {
    expect(actorRefLabel("operator:codex_cli")).toBe("Codex CLI");
    expect(actorRefLabel("operator:claude_code")).toBe("Claude Code");
    expect(actorRefLabel("operator:static-token")).toBe("static-token");
    expect(actorRefLabel("human:alice")).toBe("alice");
  });

  it("laisse intact un identifiant qui n’est pas un acteur typé", () => {
    expect(actorRefLabel("analyst:test")).toBe("analyst:test");
    expect(actorRefLabel("virustotal")).toBe("virustotal");
    expect(actorRefLabel("operator:")).toBe("operator:");
  });
});
