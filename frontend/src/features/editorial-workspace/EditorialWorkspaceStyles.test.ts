import { describe, expect, it } from "vitest";

import css from "./editorialWorkspace.css?raw";

const contrastTokenMap = {
  foregrounds: [
    "ink",
    "muted",
    "link",
    "brand",
    "warning",
    "danger",
    "technical",
  ],
  surfaces: [
    "page",
    "panel",
    "white",
    "disabled-surface",
    "outline-hover",
    "outline-selected",
    "block-selected",
    "document-hover",
  ],
  controlBoundaries: [
    "control-border",
    "divider",
    "divider-strong",
    "focus-ring",
    "attention-indicator",
  ],
} as const;

function readToken(name: string): string {
  const rootTokens = css.match(/:root\s*\{([^}]*)\}/s)?.[1];
  const declaration = rootTokens?.match(
    new RegExp(`--editorial-${name}\\s*:\\s*([^;]+);`),
  )?.[1];
  if (!declaration) {
    throw new Error(`Missing editorial contrast token: ${name}`);
  }
  const hex = declaration.match(/#[0-9a-fA-F]{6}/)?.[0];
  if (hex) return hex;

  const rgba = declaration.match(
    /rgb\(\s*(\d+)\s+(\d+)\s+(\d+)\s*\/\s*(\d+)%\s*\)/,
  );
  if (!rgba) throw new Error(`Invalid editorial contrast token: ${name}`);
  const alphaPercent = Number(rgba[4]);
  const page = readToken("page");
  const rgbChannels = [Number(rgba[1]), Number(rgba[2]), Number(rgba[3])];
  const compositedChannels = rgbChannels.map((foreground, index) => {
    const offset = [1, 3, 5][index];
    if (offset === undefined) throw new Error("Invalid RGB color token.");
    const background = Number.parseInt(page.slice(offset, offset + 2), 16);
    return Math.round(
      foreground * (alphaPercent / 100) + background * (1 - alphaPercent / 100),
    );
  });
  return `#${compositedChannels
    .map((channel) => channel.toString(16).padStart(2, "0"))
    .join("")}`;
}

function relativeLuminance(hex: string): number {
  const channels = [1, 3, 5].map(
    (offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255,
  );
  const linear = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );
  const [red, green, blue] = linear;
  if (red === undefined || green === undefined || blue === undefined) {
    throw new Error(`Invalid editorial contrast token: ${hex}`);
  }
  return red * 0.2126 + green * 0.7152 + blue * 0.0722;
}

function contrastRatio(first: string, second: string): number {
  const luminances = [relativeLuminance(first), relativeLuminance(second)].sort(
    (a, b) => b - a,
  );
  const [lighter, darker] = luminances;
  if (lighter === undefined || darker === undefined) {
    throw new Error("Could not calculate editorial contrast ratio.");
  }
  return (lighter + 0.05) / (darker + 0.05);
}

describe("editorial workspace visual accessibility", () => {
  it("keeps every declared text and surface pair at WCAG AA", () => {
    for (const foreground of contrastTokenMap.foregrounds) {
      for (const surface of contrastTokenMap.surfaces) {
        expect(
          contrastRatio(readToken(foreground), readToken(surface)),
          `${foreground} on ${surface}`,
        ).toBeGreaterThanOrEqual(4.5);
      }
    }

    expect(
      contrastRatio(readToken("white"), readToken("brand")),
    ).toBeGreaterThanOrEqual(4.5);
  });

  it("routes every stylesheet color declaration through its token map", () => {
    const rules = css.replace(/:root\s*\{[^}]*\}/s, "");
    const declarations = rules.matchAll(
      /(?:^|\s)(color|background(?:-color)?|border(?:(?:-(?:top|right|bottom|left))?(?:-color)?)?|outline)\s*:\s*([^;]+);/gm,
    );

    for (const [, property, value] of declarations) {
      expect(
        value,
        `${property} declaration must use a token or be transparent`,
      ).toMatch(/var\(--editorial-[\w-]+\)|transparent|^\s*0\s*$/);
    }
  });

  it("keeps control borders and focus rings at 3:1 on every surface", () => {
    for (const boundary of contrastTokenMap.controlBoundaries) {
      for (const surface of contrastTokenMap.surfaces) {
        expect(
          contrastRatio(readToken(boundary), readToken(surface)),
          `${boundary} on ${surface}`,
        ).toBeGreaterThanOrEqual(3);
      }
    }

    for (const scrimSurface of ["scrim-tablet", "scrim-drawer"]) {
      expect(
        contrastRatio(readToken("focus-ring"), readToken(scrimSurface)),
        `focus-ring on ${scrimSurface}`,
      ).toBeGreaterThanOrEqual(3);
    }
  });

  it("gives the technical details disclosure a 44px touch target", () => {
    const summaryRule = css.match(
      /\.editorial-technical-details\s+summary\s*\{([^}]*)\}/,
    )?.[1];
    const minimumHeight = summaryRule?.match(/min-height:\s*(\d+)px/)?.[1];

    expect(Number(minimumHeight)).toBeGreaterThanOrEqual(44);
  });
});
