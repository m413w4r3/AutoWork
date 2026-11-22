import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import {
  CHART_KEY,
  chartCorrectionProposal,
  FIGURE_A_KEY,
  FIGURE_B_KEY,
  installVisualEditorialStubs,
  VisualEditorialFixture,
} from "./fixtures/visualEditorial";

const SHOTS_DIR = resolve(process.cwd(), "../.autowork-runs/shots-f3a");

async function shot(page: Page, name: string, width: number, height: number) {
  await mkdir(SHOTS_DIR, { recursive: true });
  await page.screenshot({
    path: resolve(SHOTS_DIR, `${name}-${width}x${height}.png`),
    fullPage: false,
  });
}

function blockByText(page: Page, text: string) {
  return page
    .locator('[data-kind="paragraph"]')
    .filter({ hasText: text })
    .first();
}

function visualBlock(page: Page, elementKey: string) {
  return page.locator(
    `.editorial-document__block[data-element-key="${elementKey}"]`,
  );
}

async function captureStates(page: Page, width: number, height: number) {
  const fixture = new VisualEditorialFixture();
  await installVisualEditorialStubs(page, fixture, {
    threadProposal: chartCorrectionProposal(),
  });
  await page.setViewportSize({ width, height });
  await page.goto(`/subjects/${fixture.snapshot().subject_id}/editor`);
  await expect(page.locator(".editorial-document")).toBeVisible();

  // 1. Visual selected with its action bar.
  await visualBlock(page, CHART_KEY).click();
  await expect(
    page.getByRole("toolbar", {
      name: "Actions du visuel : Titre initial du graphique",
    }),
  ).toBeVisible();
  await shot(page, "visual-selected", width, height);

  // 2. Move mode with the provisional preview after paragraph 5.
  await visualBlock(page, FIGURE_A_KEY).click();
  await page.getByRole("button", { name: "Déplacer" }).click();
  await blockByText(page, "Paragraphe 5").click();
  await expect(
    page.getByText(
      "Insertion prévue : après « Paragraphe 5 du document synthétique. »",
    ),
  ).toBeVisible();
  await shot(page, "move-provisional-preview", width, height);
  await page.getByRole("button", { name: "Annuler" }).first().click();

  // 3. Reorder panel.
  await visualBlock(page, FIGURE_A_KEY).click();
  await page.getByRole("button", { name: "Réorganiser" }).click();
  const reorderPanel = page.getByRole("group", {
    name: "Réorganiser les visuels",
  });
  await expect(reorderPanel).toBeVisible();
  await reorderPanel.scrollIntoViewIfNeeded();
  await shot(page, "reorder-panel", width, height);
  await page.getByRole("button", { name: "Annuler" }).first().click();

  // 4. Removal confirmation dialog.
  await visualBlock(page, FIGURE_B_KEY).click();
  await page.getByRole("button", { name: "Retirer" }).click();
  await expect(
    page.getByRole("dialog", { name: "Retirer ce visuel du document ?" }),
  ).toBeVisible();
  await shot(page, "remove-dialog", width, height);
  await page.getByRole("button", { name: "Annuler" }).last().click();

  // 5. Chart correction proposal diff in the discussion.
  await visualBlock(page, CHART_KEY).click();
  await page.getByRole("button", { name: "Demander à Bridge" }).click();
  const proposalCard = page
    .getByRole("article", { name: "Proposition éditoriale" })
    .first();
  await expect(proposalCard).toBeVisible();
  await proposalCard.scrollIntoViewIfNeeded();
  await shot(page, "chart-correction-proposal", width, height);
}

test("F3-A desktop screenshots at 1280", async ({ page }) => {
  await captureStates(page, 1280, 900);
});

test("F3-A desktop screenshots at 1440", async ({ page }) => {
  await captureStates(page, 1440, 1000);
});
