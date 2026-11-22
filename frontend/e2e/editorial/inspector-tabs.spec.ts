import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import {
  CHART_KEY,
  installVisualEditorialStubs,
  VisualEditorialFixture,
} from "./fixtures/visualEditorial";

const SHOTS_DIR = resolve(process.cwd(), "../.autowork-runs/shots-r1-frontend");
const WIDTHS = [1024, 1280, 1440, 1920] as const;

interface TabsLayout {
  tabs: number;
  rowSizes: number[];
  truncated: boolean;
}

for (const width of WIDTHS) {
  test(`inspector tabs stay legible without an orphan row at ${width}px`, async ({
    page,
  }) => {
    const fixture = new VisualEditorialFixture();
    await installVisualEditorialStubs(page, fixture);
    await page.setViewportSize({ width, height: 1000 });
    await page.goto(`/subjects/${fixture.snapshot().subject_id}/editor`);
    await expect(page.locator(".editorial-document")).toBeVisible();
    await page
      .locator(`.editorial-document__block[data-element-key="${CHART_KEY}"]`)
      .click();

    const tablist = page.getByRole("tablist", {
      name: "Panneaux de l’inspecteur",
    });
    await expect(tablist).toBeVisible();
    // The full contract label is the accessible name at every width, even
    // when the visual label is abbreviated below 1280 px.
    await expect(
      page.getByRole("tab", { name: "Discussion Bridge" }),
    ).toBeVisible();

    const rawLayout: unknown = await page.evaluate(`(() => {
      const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
      const rows = new Map();
      for (const tab of tabs) {
        const top = Math.round(tab.getBoundingClientRect().top);
        rows.set(top, (rows.get(top) || 0) + 1);
      }
      return {
        tabs: tabs.length,
        rowSizes: Array.from(rows.values()),
        truncated: tabs.some((tab) => tab.scrollWidth > tab.clientWidth + 1),
      };
    })()`);
    const layout = rawLayout as TabsLayout;
    expect(layout.tabs).toBe(5);
    // Labels stay complete at every width.
    expect(layout.truncated).toBe(false);
    // Wrapping is allowed, but never a single orphan tab on its own row.
    expect(Math.min(...layout.rowSizes)).toBeGreaterThan(1);

    await mkdir(SHOTS_DIR, { recursive: true });
    await page.screenshot({
      path: resolve(SHOTS_DIR, `inspector-tabs-${width}.png`),
      fullPage: false,
    });
  });
}
