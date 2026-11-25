import { describe, expect, it } from "vitest";

import { sanitizeUrlForDisplay } from "./urlDisplay";

describe("sanitizeUrlForDisplay", () => {
  it("retains identifying parameters while stripping userinfo and fragments", () => {
    const safe = new URL(
      sanitizeUrlForDisplay(
        "https://analyst:password@source.example/report?uri=CELEX%3A32024R1234#section",
      ),
    );

    expect(safe.username).toBe("");
    expect(safe.password).toBe("");
    expect(safe.hash).toBe("");
    expect(safe.searchParams.get("uri")).toBe("CELEX:32024R1234");
  });

  it("masks credential, signing, and expiry query values", () => {
    const safe = new URL(
      sanitizeUrlForDisplay(
        "https://source.example/report?token=t&key=k&apikey=a&api_key=b&access_token=c&signature=d&sig=e&x-amz-signature=f&x-goog-credential=g&expires=h&password=i&secret=j",
      ),
    );

    for (const [, value] of safe.searchParams) {
      expect(value).toBe("REDACTED");
    }
  });

  it("masks sensitive values and removes userinfo and fragments from invalid URLs", () => {
    const safe = sanitizeUrlForDisplay(
      "https://user:password@invalid host/report?uri=CELEX%3A42&token=secret#fragment",
    );

    expect(safe).toContain("uri=CELEX%3A42");
    expect(safe).not.toMatch(/user|password|secret|fragment/);
  });

  it("preserves fragment characters in ordinary diagnostic text", () => {
    expect(sanitizeUrlForDisplay("Diagnostic #2 retained")).toBe(
      "Diagnostic #2 retained",
    );
  });
});
