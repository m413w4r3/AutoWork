import { expect, test, type Page } from "@playwright/test";

import {
  chartCorrectionProposal,
  CHART_KEY,
  DIAGRAM_KEY,
  diagramCorrectionProposal,
  FIGURE_A_KEY,
  FIGURE_B_KEY,
  installVisualEditorialStubs,
  VisualEditorialFixture,
} from "./fixtures/visualEditorial";

const VIEWPORT = { width: 1440, height: 1000 };

function blockByElementKey(page: Page, elementKey: string) {
  return page.locator(
    `.editorial-document__block[data-element-key="${elementKey}"]`,
  );
}

function captionInput(page: Page) {
  return page.getByRole("textbox", { name: "Légende éditoriale" });
}

function blockByText(page: Page, text: string) {
  return page
    .locator('[data-kind="paragraph"]')
    .filter({ hasText: text })
    .first();
}

async function openFixture(page: Page, fixture: VisualEditorialFixture) {
  await page.setViewportSize(VIEWPORT);
  await page.goto(`/subjects/${fixture.snapshot().subject_id}/editor`);
  await expect(page.locator(".editorial-document")).toBeVisible();
}

async function selectVisual(page: Page, elementKey: string) {
  await blockByElementKey(page, elementKey).click();
  await expect(
    page.locator(`[data-element-key="${elementKey}"][data-selected="true"]`),
  ).toBeVisible();
}

interface FocusInfo {
  text: string;
  kind: string | null;
  blockId: string | null;
}

async function activeFocus(page: Page): Promise<FocusInfo> {
  const serialized = await page.evaluate(`(() => {
    const element = document.activeElement;
    if (!element) return JSON.stringify({ text: "", kind: null, blockId: null });
    return JSON.stringify({
      text: (element.textContent || "").trim(),
      kind: element.dataset ? element.dataset.kind || null : null,
      blockId: element.dataset ? element.dataset.blockId || null : null,
    });
  })()`);
  return JSON.parse(String(serialized)) as FocusInfo;
}

async function pressTabUntil(
  page: Page,
  predicate: (focus: FocusInfo) => boolean,
  key: "Tab" | "Shift+Tab" = "Tab",
) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await page.keyboard.press(key);
    if (predicate(await activeFocus(page))) return;
  }
  throw new Error(`Focus target not reached by keyboard (${key}).`);
}

test("E06 — chart correction proposal compares current and proposed values, then adopts exactly one chart", async ({
  page,
}) => {
  const fixture = new VisualEditorialFixture();
  const capture = await installVisualEditorialStubs(page, fixture, {
    threadProposal: chartCorrectionProposal(),
  });
  await openFixture(page, fixture);
  await selectVisual(page, CHART_KEY);

  await page.getByRole("button", { name: "Demander à Bridge" }).click();
  await expect(
    page.getByText("Discussion sur : Titre initial du graphique — Graphique"),
  ).toBeVisible();
  await expect(page.getByLabel("Votre message à Bridge")).toBeFocused();

  const card = page
    .getByRole("article", { name: "Proposition éditoriale" })
    .first();
  await expect(card).toBeVisible();
  await expect(
    card.locator(".visual-proposal-diff__current").filter({
      hasText: "Titre initial du graphique",
    }),
  ).toBeVisible();
  await expect(
    card.locator(".visual-proposal-diff__proposed").filter({
      hasText: "Titre corrigé après revue",
    }),
  ).toBeVisible();
  await expect(card.getByText("Catégorie 1").first()).toBeVisible();
  await expect(card.getByText("Preuves utilisées")).toBeVisible();

  await card.getByRole("button", { name: "Accepter" }).click();
  await expect(card.getByRole("button", { name: "Acceptée" })).toBeVisible();
  await expect(
    page.locator(`[data-element-key="${CHART_KEY}"][data-selected="true"]`),
  ).toBeVisible();
  await expect(blockByElementKey(page, CHART_KEY)).toHaveCount(1);
  await expect(blockByElementKey(page, CHART_KEY)).toContainText(
    "Titre corrigé après revue",
  );
  await expect(page.getByText("PDF obsolète")).toBeVisible();
  expect(capture.unexpectedRequests).toEqual([]);
  expect(capture.bridgeEntries).toBe(0);
});

test("E10 — moving an image to paragraph 5 previews the destination, sends the exact anchor and never calls Bridge", async ({
  page,
}) => {
  const fixture = new VisualEditorialFixture();
  const figureBlockId = fixture.elementBlockId(FIGURE_A_KEY);
  const paragraphFiveId = fixture.paragraphBlockId(5);
  const snapshotId = fixture.snapshot().snapshot_id;
  const capture = await installVisualEditorialStubs(page, fixture);
  await openFixture(page, fixture);
  await selectVisual(page, FIGURE_A_KEY);

  await page.getByRole("button", { name: "Déplacer" }).click();
  await expect(
    page.getByRole("region", { name: "Déplacement du visuel" }),
  ).toBeVisible();
  await expect(page.getByText("Choisir un nouvel emplacement")).toBeVisible();

  await blockByText(page, "Paragraphe 5").click();
  await expect(
    page.getByText(
      "Insertion prévue : après « Paragraphe 5 du document synthétique. »",
    ),
  ).toBeVisible();

  await page.getByRole("button", { name: "Enregistrer l’emplacement" }).click();

  await expect
    .poll(() => capture.enrichmentRevisions.length)
    .toBeGreaterThan(0);
  const move = capture.enrichmentRevisions.at(-1);
  expect(move?.occurrenceId).toBe(figureBlockId);
  expect(move?.body).toEqual({
    snapshot_id: snapshotId,
    operation: "move_enrichment_element",
    element_kind: "figure",
    element_key: FIGURE_A_KEY,
    anchor_block_id: paragraphFiveId,
  });
  expect(capture.bridgeEntries).toBe(0);
  expect(
    capture.requests.some(
      (request) => request.startsWith("POST") && request.endsWith("/entries"),
    ),
  ).toBe(false);

  await expect(
    page.locator(`[data-element-key="${FIGURE_A_KEY}"][data-selected="true"]`),
  ).toBeVisible();
  await expect(page.getByText("Emplacement enregistré · v2")).toBeVisible();
  await expect(page.getByText("PDF obsolète")).toBeVisible();
  expect(capture.unexpectedRequests).toEqual([]);
});

test("E11 — swapping two images sends the exact permutation and survives a reload", async ({
  page,
}) => {
  const fixture = new VisualEditorialFixture();
  const capture = await installVisualEditorialStubs(page, fixture);
  await openFixture(page, fixture);
  await selectVisual(page, FIGURE_A_KEY);

  const anchorId = fixture.paragraphBlockId(2);
  const elementOrder = async () => {
    const raw: unknown = await page.evaluate(
      `Array.from(document.querySelectorAll('.editorial-document__block[data-element-key]')).map((block) => block.dataset.elementKey || "")`,
    );
    return raw as string[];
  };
  expect(await elementOrder()).toEqual([
    CHART_KEY,
    FIGURE_A_KEY,
    FIGURE_B_KEY,
    "diagram-a",
  ]);

  await page.getByRole("button", { name: "Réorganiser" }).click();
  const panel = page.getByRole("group", { name: "Réorganiser les visuels" });
  await expect(panel).toBeVisible();
  await expect(panel.getByText(/Ordre prévu :/)).toContainText(
    "1. Titre initial du graphique",
  );

  await page.getByRole("button", { name: `Descendre Figure A` }).click();
  await expect(panel.getByText(/Ordre prévu :/)).toContainText("3. Figure A");
  await page.getByRole("button", { name: "Enregistrer l’ordre" }).click();

  await expect
    .poll(() => capture.enrichmentRevisions.length)
    .toBeGreaterThan(0);
  const reorder = capture.enrichmentRevisions.at(-1);
  expect(reorder?.occurrenceId).toBe(anchorId);
  expect(reorder?.body).toEqual({
    snapshot_id: fixture.snapshot().snapshot_id,
    operation: "reorder_enrichment_elements",
    anchor_block_id: anchorId,
    order: [
      { kind: "chart", key: CHART_KEY },
      { kind: "figure", key: FIGURE_B_KEY },
      { kind: "figure", key: FIGURE_A_KEY },
    ],
  });

  await expect
    .poll(elementOrder)
    .toEqual([CHART_KEY, FIGURE_B_KEY, FIGURE_A_KEY, "diagram-a"]);

  await page.reload();
  await expect(page.locator(".editorial-document")).toBeVisible();
  expect(await elementOrder()).toEqual([
    CHART_KEY,
    FIGURE_B_KEY,
    FIGURE_A_KEY,
    "diagram-a",
  ]);
  expect(capture.unexpectedRequests).toEqual([]);
});

test("E13 — removal asks for confirmation, archives nothing less, and adopts the delete", async ({
  page,
}) => {
  const fixture = new VisualEditorialFixture();
  const figureBlockId = fixture.elementBlockId(FIGURE_B_KEY);
  const snapshotId = fixture.snapshot().snapshot_id;
  const capture = await installVisualEditorialStubs(page, fixture);
  await openFixture(page, fixture);
  await selectVisual(page, FIGURE_B_KEY);

  await page.getByRole("button", { name: "Retirer" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Retirer ce visuel du document ?",
  });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(
    "Retirer ce visuel du document ? Il restera dans les sources archivées et dans l’historique.",
  );
  await page.getByRole("button", { name: "Retirer le visuel" }).click();

  await expect
    .poll(() => capture.enrichmentRevisions.length)
    .toBeGreaterThan(0);
  const removal = capture.enrichmentRevisions.at(-1);
  expect(removal?.occurrenceId).toBe(figureBlockId);
  expect(removal?.body).toEqual({
    snapshot_id: snapshotId,
    operation: "remove_enrichment_element",
    element_kind: "figure",
    element_key: FIGURE_B_KEY,
  });
  await expect(blockByElementKey(page, FIGURE_B_KEY)).toHaveCount(0);
  await expect(
    page.getByText(/Il reste dans les sources archivées et dans l’historique/),
  ).toBeVisible();
  expect(capture.unexpectedRequests).toEqual([]);
});

test("E14 — direct caption save and Bridge correction are distinct paths", async ({
  page,
}) => {
  const fixture = new VisualEditorialFixture();
  const capture = await installVisualEditorialStubs(page, fixture);
  await openFixture(page, fixture);
  await selectVisual(page, FIGURE_A_KEY);

  await page.getByRole("button", { name: "Modifier la légende" }).click();
  await expect(
    page.getByText("Légende d’origine", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Légende archivée de la figure A")).toBeVisible();
  await captionInput(page).fill("Légende directe corrigée");
  await page.getByRole("button", { name: "Enregistrer" }).click();

  await expect.poll(() => capture.enrichmentRevisions.length).toBe(1);
  expect(capture.enrichmentRevisions[0]?.body).toEqual({
    snapshot_id: fixture.snapshot().snapshot_id,
    operation: "update_visual_caption",
    element_kind: "figure",
    element_key: FIGURE_A_KEY,
    caption: "Légende directe corrigée",
  });
  expect(capture.bridgeEntries).toBe(0);

  await selectVisual(page, FIGURE_A_KEY);
  await page.getByRole("button", { name: "Demander à Bridge" }).click();
  await expect(page.getByLabel("Votre message à Bridge")).toBeFocused();
  const chips = page.getByRole("group", {
    name: "Consignes de correction du visuel",
  });
  await expect(chips).toBeVisible();
  expect(capture.enrichmentRevisions).toHaveLength(1);
  expect(capture.bridgeEntries).toBe(0);

  await chips.getByRole("button", { name: "Clarifier la légende" }).click();
  await expect(page.getByLabel("Votre message à Bridge")).toHaveValue(
    "Clarifie la légende sans ajouter de fait non étayé.",
  );
  expect(capture.enrichmentRevisions).toHaveLength(1);
  expect(capture.bridgeEntries).toBe(0);
  expect(capture.unexpectedRequests).toEqual([]);
});

test("E16 — an accept conflict is explicit and leaves the document unchanged", async ({
  page,
}) => {
  const fixture = new VisualEditorialFixture();
  const capture = await installVisualEditorialStubs(page, fixture, {
    acceptConflictOnce: true,
  });
  await openFixture(page, fixture);
  await selectVisual(page, FIGURE_A_KEY);

  await page.getByRole("button", { name: "Modifier la légende" }).click();
  await captionInput(page).fill("Légende en conflit");
  await page.getByRole("button", { name: "Enregistrer" }).click();

  await expect(
    page.getByText(
      "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition.",
    ),
  ).toBeVisible();
  await expect(captionInput(page)).toHaveValue("Légende en conflit");
  expect(fixture.captions[FIGURE_A_KEY]).toBe("Figure A — légende éditoriale");
  expect(capture.unexpectedRequests).toEqual([]);
});

test("E17 — rejecting a visual proposal leaves the canonical chart unchanged", async ({
  page,
}) => {
  const fixture = new VisualEditorialFixture();
  const capture = await installVisualEditorialStubs(page, fixture, {
    threadProposal: chartCorrectionProposal(),
  });
  await openFixture(page, fixture);
  await selectVisual(page, CHART_KEY);
  await page.getByRole("button", { name: "Demander à Bridge" }).click();

  const card = page
    .getByRole("article", { name: "Proposition éditoriale" })
    .first();
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "Rejeter" }).click();
  await expect(card.getByText("Rejetée")).toBeVisible();
  await expect(card.getByRole("button", { name: "Accepter" })).toHaveCount(0);
  await expect(blockByElementKey(page, CHART_KEY)).toContainText(
    "Titre initial du graphique",
  );
  expect(fixture.chartTitle).toBe("Titre initial du graphique");
  expect(capture.enrichmentRevisions).toEqual([]);
  expect(capture.unexpectedRequests).toEqual([]);
});

test("Visuels — no mutation control is rendered when manual_edit is false", async ({
  page,
}) => {
  const fixture = new VisualEditorialFixture();
  fixture.manualEdit = false;
  const capture = await installVisualEditorialStubs(page, fixture);
  await openFixture(page, fixture);
  await selectVisual(page, CHART_KEY);

  const toolbar = page.getByRole("toolbar", {
    name: "Actions du visuel : Titre initial du graphique",
  });
  await expect(toolbar).toBeVisible();
  for (const name of [
    "Modifier la légende",
    "Déplacer",
    "Réorganiser",
    "Remplacer",
    "Retirer",
  ]) {
    await expect(toolbar.getByRole("button", { name })).toHaveCount(0);
  }
  await expect(
    toolbar.getByRole("button", { name: "Demander à Bridge" }),
  ).toBeVisible();
  await expect(toolbar.getByRole("note")).toContainText(
    "La préparation des révisions visuelles n’est pas activée pour cet atelier.",
  );
  expect(capture.enrichmentRevisions).toEqual([]);
  expect(capture.unexpectedRequests).toEqual([]);
});

test("E08 — correction d’un diagramme par proposition", async ({ page }) => {
  const fixture = new VisualEditorialFixture();
  const capture = await installVisualEditorialStubs(page, fixture, {
    threadProposal: diagramCorrectionProposal(),
  });
  await openFixture(page, fixture);
  await selectVisual(page, DIAGRAM_KEY);

  // The workshop renders a readable structured summary of the diagram.
  const summary = page.getByRole("region", {
    name: "Diagramme : Diagramme de synthèse",
  });
  await expect(summary).toBeVisible();
  await expect(
    summary.getByText("Légende initiale du diagramme"),
  ).toBeVisible();
  await expect(
    summary.getByRole("list", { name: "Nœuds du diagramme" }),
  ).toContainText("Nœud initial");
  await expect(
    summary.getByRole("list", { name: "Relations du diagramme" }),
  ).toContainText("Nœud initial → Nœud final");

  // The caption action stays a distinct manual entry; an unchanged caption
  // closes without any request.
  await page.getByRole("button", { name: "Modifier la légende" }).click();
  await expect(
    page.getByRole("textbox", { name: "Légende éditoriale" }),
  ).toHaveValue("Légende initiale du diagramme");
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("textbox", { name: "Légende éditoriale" }),
  ).toHaveCount(0);

  // The Bridge correction entry exposes the typed structure diff.
  await page.getByRole("button", { name: "Demander à Bridge" }).click();
  await expect(
    page.getByText("Discussion sur : Diagramme de synthèse — Diagramme"),
  ).toBeVisible();
  const card = page
    .getByRole("article", { name: "Proposition éditoriale" })
    .first();
  await expect(card).toBeVisible();
  await expect(
    card.getByRole("region", { name: "Comparaison du diagramme" }),
  ).toBeVisible();
  await expect(card).toContainText("Nœud corrigé après revue");
  await expect(card.getByText("Preuves du diagramme")).toBeVisible();

  await card.getByRole("button", { name: "Accepter" }).click();
  await expect(card.getByRole("button", { name: "Acceptée" })).toBeVisible();
  await expect(blockByElementKey(page, DIAGRAM_KEY)).toContainText(
    "Nœud corrigé après revue",
  );
  await expect(blockByElementKey(page, DIAGRAM_KEY)).toHaveCount(1);
  await expect(page.getByText("PDF obsolète")).toBeVisible();
  expect(capture.enrichmentRevisions).toEqual([]);
  expect(capture.bridgeEntries).toBe(0);
  expect(capture.unexpectedRequests).toEqual([]);
});

test("E19 — keyboard-only: select a visual, enter move mode, choose an anchor, confirm", async ({
  page,
}) => {
  const fixture = new VisualEditorialFixture();
  const capture = await installVisualEditorialStubs(page, fixture);
  await openFixture(page, fixture);

  const chart = blockByElementKey(page, CHART_KEY);
  await chart.focus();
  await page.keyboard.press("Enter");
  await expect(
    page.locator(`[data-element-key="${CHART_KEY}"][data-selected="true"]`),
  ).toBeVisible();

  await pressTabUntil(page, (focus) => focus.text === "Déplacer");
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("region", { name: "Déplacement du visuel" }),
  ).toBeVisible();

  await pressTabUntil(
    page,
    (focus) =>
      focus.kind === "paragraph" &&
      (focus.blockId ?? "").startsWith("paragraph-5-"),
  );
  await page.keyboard.press("Enter");
  await expect(
    page.getByText(/Insertion prévue : après « Paragraphe 5/),
  ).toBeVisible();

  await pressTabUntil(
    page,
    (focus) => focus.text === "Enregistrer l’emplacement",
  );
  await page.keyboard.press("Enter");

  await expect
    .poll(() => capture.enrichmentRevisions.length)
    .toBeGreaterThan(0);
  expect(capture.enrichmentRevisions.at(-1)?.body).toMatchObject({
    operation: "move_enrichment_element",
    element_key: CHART_KEY,
    anchor_block_id: fixture.paragraphBlockId(5),
  });
  await expect(
    page.locator(`[data-element-key="${CHART_KEY}"][data-selected="true"]`),
  ).toBeVisible();
  expect(capture.unexpectedRequests).toEqual([]);
});
