import { describe, expect, it } from "vitest";

import {
  safeHttpHref,
  sanitizeTextForDisplay,
  sanitizeUrlForDisplay,
} from "./urlDisplay";

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

  it.each([
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///tmp/report",
    "JaVaScRiPt:alert(1)",
    "java\tscript:alert(1)",
  ])("replaces non-HTTP URL schemes with a neutral label", (value) => {
    expect(sanitizeUrlForDisplay(value)).toBe("URL non affichable");
  });
});

describe("safeHttpHref", () => {
  it.each([
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///tmp/report",
    "JaVaScRiPt:alert(1)",
    "java\tscript:alert(1)",
    "//source.example/report",
    "/subjects/123",
    "source.example/report",
  ])("rejects unsafe or non-absolute URL inputs", (value) => {
    expect(safeHttpHref(value)).toBeNull();
  });

  it.each(["http", "https"] as const)(
    "keeps a sanitized %s URL for display",
    (protocol) => {
      const href = safeHttpHref(
        `${protocol}://analyst:password@source.example/report?uri=CELEX%3A42&token=secret&X-Amz-Signature=signed#section`,
      );

      expect(href).not.toBeNull();
      const safe = new URL(href ?? "");
      expect(safe.protocol).toBe(`${protocol}:`);
      expect(safe.username).toBe("");
      expect(safe.password).toBe("");
      expect(safe.hash).toBe("");
      expect(safe.searchParams.get("uri")).toBe("CELEX:42");
      expect(safe.searchParams.get("token")).toBe("REDACTED");
      expect(safe.searchParams.get("X-Amz-Signature")).toBe("REDACTED");
      expect(href).not.toContain("password");
      expect(href).not.toContain("signed");
    },
  );
});

describe("sanitizeTextForDisplay", () => {
  it("redacts URLs embedded in free text and bounds the result", () => {
    const text = sanitizeTextForDisplay(
      "Request failed for https://analyst:password@source.example/report?token=secret&ref=ok#fragment",
    );

    expect(text).toContain(
      "https://source.example/report?token=REDACTED&ref=ok",
    );
    expect(text).not.toMatch(/analyst|password|secret|fragment/);
    expect(sanitizeTextForDisplay("x".repeat(600))).toHaveLength(500);
  });
});

describe("embedded non-http URLs in free text", () => {
  it("never leaves a javascript: URL with a secret query inside prose", () => {
    const text = sanitizeTextForDisplay(
      "échec sur javascript:alert(1)?token=abc123 puis data:text/html,secret=zzz fin",
    );

    expect(text).not.toContain("javascript:");
    expect(text).not.toContain("abc123");
    expect(text).not.toContain("data:text");
    expect(text).toContain("échec sur");
  });
});
