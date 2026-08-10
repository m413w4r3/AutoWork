/** Sérialiseur DOM pur et testable, chargé avant content.js. */
(() => {
  "use strict";

  const SERIALIZER_VERSION = "chatgpt-dom-v2";
  const BLOCK_TAGS = new Set([
    "P",
    "DIV",
    "H1",
    "H2",
    "H3",
    "H4",
    "H5",
    "H6",
    "BLOCKQUOTE",
    "TABLE",
    "TR",
    "HR",
  ]);
  const BUTTON_WORDS = new Set([
    "copier",
    "copy",
    "run",
    "exécuter",
    "executer",
    "edit",
    "éditer",
  ]);
  const TRACKING_KEYS = new Set(["fbclid", "gclid"]);

  function canonicalizeHttpsUrl(raw) {
    let parsed;
    try {
      parsed = new URL(raw);
    } catch {
      return null;
    }
    if (parsed.protocol !== "https:" || parsed.username || parsed.password) return null;
    parsed.hash = "";
    for (const key of [...parsed.searchParams.keys()]) {
      if (key.toLowerCase().startsWith("utm_") || TRACKING_KEYS.has(key.toLowerCase())) {
        parsed.searchParams.delete(key);
      }
    }
    const entries = [...parsed.searchParams.entries()].sort(([leftKey, leftValue], [rightKey, rightValue]) =>
      leftKey === rightKey ? leftValue.localeCompare(rightValue) : leftKey.localeCompare(rightKey),
    );
    parsed.search = "";
    for (const [key, value] of entries) parsed.searchParams.append(key, value);
    return parsed.toString();
  }

  function isCitationControl(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return false;
    const marker = `${node.getAttribute("data-testid") || ""} ${node.className || ""} ${
      node.getAttribute("aria-label") || ""
    }`.toLowerCase();
    return (
      node.tagName === "SUP" ||
      /citation|source-pill|webpage-source/.test(marker) ||
      Boolean(node.closest("[data-testid*='citation'], [data-testid*='source-pill']"))
    );
  }

  function codeLanguage(pre, code) {
    const match = code && String(code.className || "").match(/language-([\w+-]+)/);
    if (match) return match[1];
    const header = pre.querySelector("div");
    if (!header) return "";
    const copy = header.cloneNode(true);
    copy.querySelectorAll("button").forEach((button) => button.remove());
    const word = (copy.textContent.trim().split(/\s+/)[0] || "").toLowerCase();
    return BUTTON_WORDS.has(word) || !/^[a-z][\w+#-]*$/.test(word) ? "" : word;
  }

  function citationFrom(node) {
    const anchor = node.matches?.("a[href]") ? node : node.querySelector?.("a[href]");
    if (!anchor) return null;
    const rawUrl = anchor.getAttribute("href") || "";
    const canonicalUrl = canonicalizeHttpsUrl(rawUrl);
    if (!canonicalUrl) return null;
    const label = (node.innerText || node.textContent || anchor.textContent || "")
      .replace(/\s+/g, " ")
      .trim();
    return {
      label: label || new URL(canonicalUrl).hostname,
      url: rawUrl,
      canonical_url: canonicalUrl,
      position: null,
    };
  }

  function serializeResponse(root, openPre = null) {
    const citationsByUrl = new Map();

    function visit(node) {
      if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || "";
      if (node.nodeType !== Node.ELEMENT_NODE) return "";
      const tag = node.tagName;
      if (isCitationControl(node)) {
        const citation = citationFrom(node);
        if (citation && !citationsByUrl.has(citation.canonical_url)) {
          citationsByUrl.set(citation.canonical_url, citation);
        }
        return "";
      }
      if (
        tag === "BUTTON" ||
        tag === "SVG" ||
        node.classList.contains("sr-only") ||
        node.hasAttribute("aria-live")
      ) {
        return "";
      }
      if (tag === "BR") return "\n";
      if (tag === "PRE") {
        const code = node.querySelector("code");
        const raw = (code ? code.textContent : node.textContent).replace(/\n+$/, "");
        const language = codeLanguage(node, code);
        if (node === openPre) return language ? `\n\`\`\`${language}\n${raw}` : "";
        return `\n\`\`\`${language}\n${raw}\n\`\`\`\n`;
      }

      let output = "";
      for (const child of node.childNodes) output += visit(child);
      if (tag === "A") {
        const rawUrl = node.getAttribute("href") || "";
        const destination = canonicalizeHttpsUrl(rawUrl);
        return destination && output.trim() ? `[${output.trim()}](${rawUrl})` : output;
      }
      if (tag === "CODE") return `\`${output}\``;
      if (tag === "LI") return `\n- ${output.trim()}`;
      if (tag === "TD" || tag === "TH") return `${output} | `;
      if (BLOCK_TAGS.has(tag)) return `\n${output}\n`;
      return output;
    }

    const text = visit(root)
      .replace(/[ \t]+$/gm, "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    return {
      text,
      visible_citations: [...citationsByUrl.values()],
      serializer_version: SERIALIZER_VERSION,
    };
  }

  globalThis.ChatGPTBridgeSerializer = {
    SERIALIZER_VERSION,
    canonicalizeHttpsUrl,
    isCitationControl,
    serializeResponse,
  };
})();
