import {
  expect,
  test,
  type Page,
  type Request,
  type Response,
} from "@playwright/test";

/**
 * M23 real-backend editorial smoke.
 *
 * These specs run against the launcher in
 * `backend/tests/workflow_emulator/editorial_browser_server.py`: a throwaway
 * PostgreSQL database, the real FastAPI application, the F5 acceptance seed and
 * a no-call model gateway. The same browser code paths the stubbed editorial
 * suite exercises are driven here, so response-shape drift between the typed
 * frontend client and the real routes fails loudly.
 *
 * The launcher publishes the synthetic fixture state under
 * `/api/editorial-browser/state`: six paragraphs, one archived table and four
 * archived figures (three integrated through the real manual routes, one left
 * available for replacement).
 */

interface BrowserParagraph {
  block_id: string;
  kind: string;
  text: string;
}

interface BrowserFigure {
  original_name: string;
  caption: string;
  element_key: string;
  block_id: string;
  anchor_block_id: string;
}

interface BrowserState {
  subject_id: string;
  publication_kind: string;
  paragraphs: BrowserParagraph[];
  figures: BrowserFigure[];
  table: { element_key: string | null; block_id: string | null };
  spare_figure: { original_name: string; figure_id: string };
  snapshot_id: string;
  enrichment_version: number;
  publication_version: number;
}

interface SnapshotFigureProjection {
  catalog_figure_id: string | null;
  caption: string;
  anchor_block_id: string | null;
}

interface SnapshotBlock {
  block_id: string;
  kind: string;
  text: string | null;
  element_kind?: string | null;
  element_key?: string | null;
  allowed_operations: string[];
  figure?: SnapshotFigureProjection | null;
}

interface Snapshot {
  subject_id: string;
  snapshot_id: string;
  publication_kind: string | null;
  preview_status: string;
  pdf_status: string;
  enrichment_version: number;
  publication_version: number;
  capabilities: Record<string, boolean | string | null>;
  blocks: SnapshotBlock[];
  enrichments: SnapshotBlock[];
}

interface CatalogItem {
  figure_id: string;
  original_name: string;
  status: string;
  selectable: boolean;
}

interface NetworkCapture {
  requests: string[];
  responses: Array<{ method: string; url: string; status: number }>;
}

/** Bridge turns and model calls must never happen during manual operations. */
const MODEL_OR_BRIDGE_ENDPOINT =
  /\/editor\/threads\/[^/]+\/entries(?:[?#]|$)|\/api\/(?:v1\/)?(?:model|bridge)[^/]*/i;

function isRealBackendLite(): boolean {
  return process.env.EDITORIAL_PLAYWRIGHT_MODE === "real-backend-lite";
}

function requireRealBackendLite(): void {
  test.skip(
    !isRealBackendLite(),
    "Set EDITORIAL_PLAYWRIGHT_MODE=real-backend-lite to select this suite.",
  );
  test.skip(
    process.env.EDITORIAL_REAL_LITE_ATTESTED_FAKE_PROVIDER !== "1",
    "Operator must attest the PostgreSQL test backend and fake provider with EDITORIAL_REAL_LITE_ATTESTED_FAKE_PROVIDER=1.",
  );
}

test.beforeEach(() => {
  requireRealBackendLite();
});

function captureNetwork(page: Page): NetworkCapture {
  const capture: NetworkCapture = { requests: [], responses: [] };
  page.on("request", (request: Request) => {
    capture.requests.push(`${request.method()} ${request.url()}`);
  });
  page.on("response", (response: Response) => {
    capture.responses.push({
      method: response.request().method(),
      url: response.url(),
      status: response.status(),
    });
  });
  return capture;
}

function expectNoModelOrBridgeCall(capture: NetworkCapture): void {
  const forbidden = capture.requests.filter((request) =>
    MODEL_OR_BRIDGE_ENDPOINT.test(request),
  );
  expect(
    forbidden,
    "manual editorial operations must never submit a Bridge turn or call a model",
  ).toEqual([]);
}

/**
 * Assert every captured response whose path matches `pathPattern` answered 2xx.
 * A drifted request body surfaces as 4xx here instead of only as a missing UI
 * element.
 */
function expectOkResponses(capture: NetworkCapture, pathPattern: RegExp): void {
  const matching = capture.responses.filter((response) =>
    pathPattern.test(new URL(response.url).pathname),
  );
  expect(
    matching.length,
    `expected at least one response matching ${pathPattern}`,
  ).toBeGreaterThan(0);
  for (const response of matching) {
    expect(
      response.status,
      `${response.method} ${response.url} must answer 2xx`,
    ).toBeGreaterThanOrEqual(200);
    expect(
      response.status,
      `${response.method} ${response.url} must answer 2xx`,
    ).toBeLessThan(300);
  }
}

/** Wait for the operation response, then assert it answered 2xx. */
async function expectOperationResponses(
  capture: NetworkCapture,
  pathPattern: RegExp,
): Promise<void> {
  await expect
    .poll(
      () =>
        capture.responses.filter((response) =>
          pathPattern.test(new URL(response.url).pathname),
        ).length,
      { message: `waiting for a response matching ${pathPattern}` },
    )
    .toBeGreaterThan(0);
  expectOkResponses(capture, pathPattern);
}

async function browserState(page: Page): Promise<BrowserState> {
  const response = await page.request.get("/api/editorial-browser/state");
  expect(response.status(), "launcher state must be readable").toBe(200);
  return (await response.json()) as BrowserState;
}

async function getSnapshot(page: Page, subjectId: string): Promise<Snapshot> {
  const response = await page.request.get(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/snapshot`,
  );
  expect(
    response.status(),
    "the real editorial snapshot API must answer 2xx",
  ).toBe(200);
  return (await response.json()) as Snapshot;
}

async function getCatalog(
  page: Page,
  subjectId: string,
  status: string,
): Promise<CatalogItem[]> {
  const response = await page.request.get(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/figures?status=${status}`,
  );
  expect(response.status(), "the real figure catalog API must answer 2xx").toBe(
    200,
  );
  const payload = (await response.json()) as { items: CatalogItem[] };
  return payload.items;
}

async function openEditor(page: Page, subjectId: string): Promise<void> {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${encodeURIComponent(subjectId)}/editor`);
  await expect(page.locator(".editorial-document")).toBeVisible();
}

function blockByElementKey(page: Page, elementKey: string) {
  return page.locator(
    `.editorial-document__block[data-element-key="${elementKey}"]`,
  );
}

function paragraphBlockByText(page: Page, text: string) {
  return page
    .locator('.editorial-document__block[data-kind="paragraph"]')
    .filter({ hasText: text })
    .first();
}

async function expectPdfStale(page: Page): Promise<void> {
  await expect(page.locator(".editorial-status")).toHaveText("PDF obsolète");
}

test("real-backend smoke: the seeded PostgreSQL workspace renders in the browser", async ({
  page,
}) => {
  const capture = captureNetwork(page);
  const state = await browserState(page);
  await openEditor(page, state.subject_id);

  await expect(
    page
      .locator("article.editorial-document")
      .getByRole("heading", { name: "[Synthetic actor] Acceptance report" }),
  ).toBeVisible();
  for (const paragraph of state.paragraphs) {
    await expect(
      page
        .locator(".editorial-document__block")
        .filter({ hasText: paragraph.text })
        .first(),
    ).toBeVisible();
  }
  for (const figure of state.figures) {
    await expect(blockByElementKey(page, figure.element_key)).toContainText(
      figure.caption,
    );
  }

  const snapshot = await getSnapshot(page, state.subject_id);
  expect(snapshot.publication_kind).toBe(state.publication_kind);
  expect(snapshot.capabilities.manual_edit).toBe(true);
  expect(snapshot.capabilities.read_only_reason).toBeNull();
  expect(
    snapshot.blocks.filter((block) =>
      ["paragraph", "lead_paragraph"].includes(block.kind),
    ),
  ).toHaveLength(6);
  expectNoModelOrBridgeCall(capture);
});

test("E01 inline edit of a paragraph is saved canonically and survives a reload", async ({
  page,
}) => {
  const capture = captureNetwork(page);
  const state = await browserState(page);
  const paragraph = state.paragraphs[2];
  const revised = `${paragraph.text} Synthetic report revision.`;
  await openEditor(page, state.subject_id);

  const block = paragraphBlockByText(page, paragraph.text);
  await block.click();
  await expect(block).toHaveAttribute("data-selected", "true");
  const toolbar = page.getByRole("toolbar", { name: /Actions du bloc/ });
  await toolbar.getByRole("button", { name: "Modifier" }).click();
  const editor = page.getByRole("textbox", { name: "Texte du bloc" });
  await expect(editor).toBeFocused();
  await editor.fill(revised);
  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();

  await expectOperationResponses(
    capture,
    /\/editor\/blocks\/[^/]+\/manual-edit$/,
  );
  await expect(page.getByText(/Enregistré · v\d+/)).toBeVisible();
  await expectPdfStale(page);
  await page.reload();
  await expect(page.locator(".editorial-document")).toBeVisible();
  await expect(
    page
      .locator(".editorial-document__block")
      .filter({ hasText: revised })
      .first(),
  ).toBeVisible();

  expectNoModelOrBridgeCall(capture);
  const snapshot = await getSnapshot(page, state.subject_id);
  const persisted = snapshot.blocks.find(
    (candidate) => candidate.block_id === paragraph.block_id,
  );
  expect(persisted?.text).toBe(revised);
  const pdf = await page.request.get(
    `/api/subjects/${encodeURIComponent(state.subject_id)}/editor/pdf`,
  );
  expect(pdf.status()).toBe(200);
  expect(((await pdf.json()) as { status: string }).status).toBe("STALE");
});

test("E01b the lead paragraph can be edited from the document action bar", async ({
  page,
}) => {
  const capture = captureNetwork(page);
  const state = await browserState(page);
  const lead = state.paragraphs[0];
  const revised = `${lead.text} Synthetic report revision.`;
  await openEditor(page, state.subject_id);

  const block = page
    .locator('.editorial-document__block[data-kind="lead_paragraph"]')
    .filter({ hasText: lead.text })
    .first();
  await block.click();
  const toolbar = page.getByRole("toolbar", { name: /Actions du bloc/ });
  await toolbar.getByRole("button", { name: "Modifier" }).click();
  const editor = page.getByRole("textbox", { name: "Texte du bloc" });
  await editor.fill(revised);
  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();

  await expectOperationResponses(
    capture,
    /\/editor\/blocks\/[^/]+\/manual-edit$/,
  );
  await expect(page.getByText(/Enregistré · v\d+/)).toBeVisible();
  expectNoModelOrBridgeCall(capture);
  const snapshot = await getSnapshot(page, state.subject_id);
  expect(
    snapshot.blocks.find((candidate) => candidate.block_id === lead.block_id)
      ?.text,
  ).toBe(revised);
});

test("E10 moving a figure after another paragraph persists and marks the PDF stale", async ({
  page,
}) => {
  const capture = captureNetwork(page);
  const state = await browserState(page);
  const figure = state.figures[0];
  const target = state.paragraphs[4];
  await openEditor(page, state.subject_id);

  const block = blockByElementKey(page, figure.element_key);
  await block.click();
  await expect(
    page.locator(
      `[data-element-key="${figure.element_key}"][data-selected="true"]`,
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Déplacer" }).click();
  await expect(
    page.getByRole("region", { name: "Déplacement du visuel" }),
  ).toBeVisible();
  await page
    .locator(".editorial-document__block[data-move-target]")
    .filter({ hasText: target.text })
    .first()
    .click();
  await expect(
    page.getByText(`Insertion prévue : après « ${target.text} »`),
  ).toBeVisible();
  await page.getByRole("button", { name: "Enregistrer l’emplacement" }).click();

  await expectOperationResponses(
    capture,
    /\/editor\/blocks\/[^/]+\/enrichment-revisions$/,
  );
  await expectOperationResponses(capture, /\/proposals\/[^/]+\/accept$/);
  await expect(page.getByText(/Emplacement enregistré · v\d+/)).toBeVisible();
  await expectPdfStale(page);
  expectNoModelOrBridgeCall(capture);

  await page.reload();
  await expect(page.locator(".editorial-document")).toBeVisible();
  const rawPositions: unknown = await page.evaluate(
    `(() => {
      const blocks = Array.from(document.querySelectorAll('.editorial-document__block'));
      return {
        figure: blocks.findIndex(
          (candidate) =>
            candidate.dataset.elementKey === ${JSON.stringify(figure.element_key)},
        ),
        paragraph: blocks.findIndex(
          (candidate) =>
            candidate.dataset.kind === 'paragraph' &&
            (candidate.textContent || '').includes(${JSON.stringify(target.text)}),
        ),
      };
    })()`,
  );
  const positions = rawPositions as { figure: number; paragraph: number };
  expect(positions.figure).toBeGreaterThanOrEqual(0);
  expect(positions.paragraph).toBeGreaterThanOrEqual(0);
  expect(positions.figure).toBe(positions.paragraph + 1);

  const snapshot = await getSnapshot(page, state.subject_id);
  const moved = snapshot.enrichments.filter(
    (candidate) => candidate.element_key === figure.element_key,
  );
  expect(moved).toHaveLength(1);
  expect(moved[0]?.figure?.anchor_block_id).toBe(target.block_id);
});

test("E11 reordering two figures after the same paragraph survives a reload", async ({
  page,
}) => {
  const capture = captureNetwork(page);
  const state = await browserState(page);
  const shared = state.figures.filter(
    (figure) => figure.anchor_block_id === state.paragraphs[3].block_id,
  );
  expect(
    shared,
    "the launcher must integrate two figures after the same paragraph",
  ).toHaveLength(2);
  const [first, second] = shared;
  await openEditor(page, state.subject_id);

  await blockByElementKey(page, first.element_key).click();
  await page.getByRole("button", { name: "Réorganiser" }).click();
  const panel = page.getByRole("group", { name: "Réorganiser les visuels" });
  await expect(panel).toBeVisible();
  await expect(panel.getByText(/Ordre prévu :/)).toContainText(
    `1. ${first.caption}`,
  );
  await page
    .getByRole("button", { name: `Descendre ${first.caption}` })
    .click();
  await expect(panel.getByText(/Ordre prévu :/)).toContainText(
    `2. ${first.caption}`,
  );
  await page.getByRole("button", { name: "Enregistrer l’ordre" }).click();

  await expectOperationResponses(
    capture,
    /\/editor\/blocks\/[^/]+\/enrichment-revisions$/,
  );
  await expectOperationResponses(capture, /\/proposals\/[^/]+\/accept$/);
  await expect(page.getByText(/Ordre enregistré · v\d+/)).toBeVisible();
  expectNoModelOrBridgeCall(capture);

  await page.reload();
  await expect(page.locator(".editorial-document")).toBeVisible();
  const rawOrder: unknown = await page.evaluate(
    `Array.from(document.querySelectorAll('.editorial-document__block[data-element-key]')).map((candidate) => candidate.dataset.elementKey || '')`,
  );
  const order = rawOrder as string[];
  expect(order.indexOf(second.element_key)).toBeGreaterThanOrEqual(0);
  expect(order.indexOf(first.element_key)).toBeGreaterThan(
    order.indexOf(second.element_key),
  );

  const snapshot = await getSnapshot(page, state.subject_id);
  const persisted = snapshot.enrichments
    .filter((candidate) =>
      [first.element_key, second.element_key].includes(
        candidate.element_key ?? "",
      ),
    )
    .map((candidate) => candidate.element_key);
  expect(persisted).toEqual([second.element_key, first.element_key]);
});

test("E12 replacing a figure through the gallery keeps exactly one image at that location", async ({
  page,
}) => {
  const capture = captureNetwork(page);
  const state = await browserState(page);
  const shared = state.figures.filter(
    (figure) => figure.anchor_block_id === state.paragraphs[3].block_id,
  );
  const target = shared[0];
  const spare = state.spare_figure;
  await openEditor(page, state.subject_id);

  const block = blockByElementKey(page, target.element_key);
  await block.click();
  await page.getByRole("button", { name: "Remplacer" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Remplacer l’image intégrée",
  });
  await expect(dialog).toBeVisible();
  await dialog
    .getByRole("radio", { name: `Choisir ${spare.original_name}` })
    .check();
  await expect(
    dialog.getByRole("region", { name: "Comparaison des images" }),
  ).toContainText(spare.original_name);
  await dialog.getByRole("button", { name: "Remplacer l’image" }).click();

  await expectOperationResponses(
    capture,
    /\/editor\/blocks\/[^/]+\/enrichment-revisions$/,
  );
  await expectOperationResponses(capture, /\/proposals\/[^/]+\/accept$/);
  await expect(
    dialog.getByText(
      "Image remplacée. Le document contient toujours exactement une image à cet emplacement.",
    ),
  ).toBeVisible();
  expectNoModelOrBridgeCall(capture);
  await dialog
    .getByRole("button", { name: "Fermer le remplacement d’image" })
    .click();

  const snapshot = await getSnapshot(page, state.subject_id);
  const replaced = snapshot.enrichments.filter(
    (candidate) => candidate.element_key === target.element_key,
  );
  expect(replaced).toHaveLength(1);
  expect(replaced[0]?.figure?.catalog_figure_id).toBe(spare.figure_id);
  expect(
    snapshot.enrichments.filter((candidate) => candidate.kind === "figure"),
  ).toHaveLength(3);
  const available = await getCatalog(page, state.subject_id, "available");
  expect(
    available.map((item) => item.original_name),
    "the replaced archive entry must return to the gallery",
  ).toContain(target.original_name);
  expect(
    available.map((item) => item.original_name),
    "the spare archive entry is now integrated",
  ).not.toContain(spare.original_name);
});

test("E13 removing a figure asks for confirmation and keeps its archive entry", async ({
  page,
}) => {
  const capture = captureNetwork(page);
  const state = await browserState(page);
  const shared = state.figures.filter(
    (figure) => figure.anchor_block_id === state.paragraphs[3].block_id,
  );
  const target = shared[1];
  await openEditor(page, state.subject_id);

  const block = blockByElementKey(page, target.element_key);
  await block.click();
  await page.getByRole("button", { name: "Retirer" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Retirer ce visuel du document ?",
  });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(
    "Il restera dans les sources archivées et dans l’historique.",
  );
  await dialog.getByRole("button", { name: "Retirer le visuel" }).click();

  await expectOperationResponses(
    capture,
    /\/editor\/blocks\/[^/]+\/enrichment-revisions$/,
  );
  await expectOperationResponses(capture, /\/proposals\/[^/]+\/accept$/);
  await expect(
    page.getByText(
      /Visuel retiré du document · v\d+\. Il reste dans les sources archivées et dans l’historique\./,
    ),
  ).toBeVisible();
  expectNoModelOrBridgeCall(capture);
  await expect(blockByElementKey(page, target.element_key)).toHaveCount(0);

  const snapshot = await getSnapshot(page, state.subject_id);
  expect(
    snapshot.enrichments.filter(
      (candidate) => candidate.element_key === target.element_key,
    ),
  ).toHaveLength(0);
  const available = await getCatalog(page, state.subject_id, "available");
  expect(
    available.map((item) => item.original_name),
    "the removed figure archive entry must stay listed",
  ).toContain(target.original_name);
});
