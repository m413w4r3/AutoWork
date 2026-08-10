import { beforeAll, describe, expect, it } from "vitest";

import serializerSource from "../../chatgpt-bridge/extension/serializer.js?raw";

interface SerializerResult {
  text: string;
  visible_citations: Array<{
    label: string;
    url: string;
    canonical_url: string;
    position: number | null;
  }>;
  serializer_version: string;
}

interface BridgeSerializer {
  serializeResponse(root: Element): SerializerResult;
}

declare global {
  var ChatGPTBridgeSerializer: BridgeSerializer;
}

beforeAll(() => {
  (0, eval)(serializerSource);
});

function serialize(html: string): SerializerResult {
  const root = document.createElement("div");
  root.innerHTML = html;
  return globalThis.ChatGPTBridgeSerializer.serializeResponse(root);
}

describe("ChatGPT DOM serializer", () => {
  it("keeps citation pills out of words and returns them separately", () => {
    const result = serialize(`
      <p>Le groupe utili<span data-testid="webpage-citation-pill">
        <a href="https://publisher.example/report?utm_source=chatgpt">Publisher</a>
      </span>se un chargeur.</p>
      <span data-testid="webpage-citation-pill">
        <a href="https://second.example/advisory">+1</a>
      </span>
    `);

    expect(result.text).toContain("Le groupe utilise un chargeur.");
    expect(result.text).not.toContain("Publisher");
    expect(result.text).not.toContain("+1");
    expect(result.visible_citations).toEqual([
      {
        label: "Publisher",
        url: "https://publisher.example/report?utm_source=chatgpt",
        canonical_url: "https://publisher.example/report",
        position: null,
      },
      {
        label: "+1",
        url: "https://second.example/advisory",
        canonical_url: "https://second.example/advisory",
        position: null,
      },
    ]);
    expect(result.serializer_version).toBe("chatgpt-dom-v2");
  });

  it("keeps ordinary HTTPS links and rejects unsafe citation destinations", () => {
    const result = serialize(`
      <p>Lire <a href="https://cert.example/advisory">l'avis du CERT</a>.</p>
      <span data-testid="citation"><a href="http://unsafe.example/">Unsafe</a></span>
    `);

    expect(result.text).toContain(
      "Lire [l'avis du CERT](https://cert.example/advisory).",
    );
    expect(result.visible_citations).toEqual([]);
  });
});
