import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page, Route } from "@playwright/test";

import type {
  EditorialFigureCatalogItem,
  EditorialManualSourceFigureProposalInput,
  EditorialProposalRead,
  EditorialVisualRevisionProposalRead,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
  fulfillEditorialReadStatuses,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const subjectPath = `/api/subjects/${subjectId}`;
const snapshotPath = `${subjectPath}/editor/snapshot`;
const figuresPath = `${subjectPath}/editor/figures`;
const screenshotDirectory = resolve(
  process.cwd(),
  "../.autowork-runs/shots-f3b",
);

const anchorId = "11111111-1111-4111-8111-111111111111";
const secondParagraphId = "22222222-2222-4222-8222-222222222222";
const figureBlockId = "33333333-3333-4333-8333-333333333333";
const chartBlockId = "37373737-3737-4737-8737-373737373737";
const chartKey = "chart-volume-1";
const elementKey = "adopted_source_figure_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const sourceA = "44444444-4444-4444-8444-444444444444";
const sourceB = "55555555-5555-4555-8555-555555555555";
const figureIds = {
  a1: "66666666-6666-4666-8666-666666666666",
  a2: "77777777-7777-4777-8777-777777777777",
  b1: "88888888-8888-4888-8888-888888888888",
  b2: "99999999-9999-4999-8999-999999999999",
  ineligible: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  alreadyUsed: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
} as const;
const proposalId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const revisionProposalId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";

const anchorText =
  "Paragraphe d’ancrage synthétique qui reçoit les images sources.";
const figureCaption = "Légende actuelle de la figure intégrée";

function catalogItem(
  figureId: string,
  name: string,
  sourceIdValue: string,
  status: EditorialFigureCatalogItem["status"],
  selectable: boolean,
  exclusionReason: string | null = null,
): EditorialFigureCatalogItem {
  return {
    figure_id: figureId,
    original_name: name,
    original_caption: `Légende archivée de ${name}`,
    alt_text: name,
    source: {
      source_id: sourceIdValue,
      title:
        sourceIdValue === sourceA
          ? "Rapport public source A"
          : "Rapport public source B",
      url: "https://publisher.example/report",
    },
    page: 4,
    section: "Infrastructure",
    context_before: "Contexte précédent conservé dans l’archive.",
    context_after: "Contexte suivant conservé dans l’archive.",
    width: 1280,
    height: 720,
    mime_type: "image/png",
    status,
    selectable,
    exclusion_reason: exclusionReason,
    already_used: status === "already_integrated",
  };
}

const catalog: EditorialFigureCatalogItem[] = [
  catalogItem(
    figureIds.a1,
    "Carte des flux réseau",
    sourceA,
    "available",
    true,
  ),
  catalogItem(
    figureIds.a2,
    "Schéma de l’infrastructure",
    sourceA,
    "available",
    true,
  ),
  catalogItem(
    figureIds.b1,
    "Photographie du serveur",
    sourceB,
    "available",
    true,
  ),
  catalogItem(
    figureIds.b2,
    "Capture de configuration",
    sourceB,
    "available",
    true,
  ),
  catalogItem(
    figureIds.ineligible,
    "Capture non admissible",
    sourceB,
    "ineligible",
    false,
    "La licence de cette figure ne permet pas son intégration.",
  ),
  catalogItem(
    figureIds.alreadyUsed,
    "Figure déjà intégrée",
    sourceA,
    "already_integrated",
    false,
  ),
];

function paragraphBlock(
  blockId: string,
  position: number,
  text: string,
  operations: string[],
): EditorialWorkspaceBlock {
  return {
    block_id: blockId,
    kind: "paragraph",
    parent_block_id: null,
    position,
    text,
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: "a".repeat(64),
    allowed_operations: operations,
    revision_status: "ready",
  };
}

function integratedFigureBlock(position: number): EditorialWorkspaceBlock {
  return {
    block_id: figureBlockId,
    kind: "figure",
    parent_block_id: null,
    position,
    text: figureCaption,
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: figureBlockId, legacy_target_id: null },
    before_hash: "b".repeat(64),
    allowed_operations: [
      "replace_source_figure",
      "update_visual_caption",
      "move_enrichment_element",
      "delete_source_figure",
    ],
    revision_status: "ready",
    element_kind: "figure",
    element_key: elementKey,
    caption: figureCaption,
    figure: {
      key: elementKey,
      catalog_figure_id: figureIds.alreadyUsed,
      caption: figureCaption,
      source_url: "https://publisher.example/report",
      source: "Rapport public source A",
      provenance: "Figure archivée, vérifiée et conservée dans l’inventaire.",
      sha256: "c".repeat(64),
      mime_type: "image/png",
      byte_size: 4096,
      width: 1280,
      height: 720,
      status: "included",
      order: 0,
      anchor_block_id: anchorId,
      placement: "after_paragraph",
      page: 4,
      section: "Infrastructure",
    },
  };
}

function chartWorkspaceBlock(position: number): EditorialWorkspaceBlock {
  return {
    block_id: chartBlockId,
    kind: "chart",
    parent_block_id: null,
    position,
    text: "Graphique des volumes observés",
    evidence_refs: [],
    chart: {
      key: chartKey,
      kind: "bar",
      title: "Graphique des volumes observés",
      caption: "Volumes observés dans les sources synthétiques.",
      legend: ["Série A"],
      category_axis_label: "Catégorie",
      value_axis_label: "Valeur",
      unit: "unité",
      points: [
        {
          label: "Catégorie 1",
          value: "12",
          evidence_refs: [],
        },
      ],
      anchor_block_id: anchorId,
    },
    revision_target: { block_id: chartBlockId, legacy_target_id: null },
    before_hash: "f".repeat(64),
    allowed_operations: [
      "rewrite_chart",
      "update_visual_caption",
      "move_enrichment_element",
      "delete_chart",
    ],
    revision_status: "ready",
    element_kind: "chart",
    element_key: chartKey,
    caption: "Volumes observés dans les sources synthétiques.",
  };
}

function createSnapshot(
  kind: "article" | "brief",
  withIntegratedFigure: boolean,
  withChart = false,
): EditorialWorkspaceSnapshot {
  const blocks: EditorialWorkspaceBlock[] = [
    paragraphBlock(anchorId, 0, anchorText, [
      "rewrite_paragraph",
      "insert_after",
      "create_source_figure",
    ]),
  ];
  if (withIntegratedFigure) blocks.push(integratedFigureBlock(1));
  if (withChart) blocks.push(chartWorkspaceBlock(blocks.length));
  blocks.push(
    paragraphBlock(
      secondParagraphId,
      blocks.length,
      "Paragraphe suivant conservé pour vérifier la position de lecture.",
      ["rewrite_paragraph"],
    ),
  );
  return {
    subject_id: subjectId,
    production_run_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    publication_kind: kind,
    synthesis_artifact_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
    synthesis_version: 4,
    synthesis_canonical_sha256: "a".repeat(64),
    enrichment_artifact_id: "12121212-1212-4212-8212-121212121212",
    enrichment_version: 2,
    publication_artifact_id: null,
    publication_version: null,
    pipeline_generation: 1,
    blocks,
    enrichments: withIntegratedFigure ? [integratedFigureBlock(1)] : [],
    document_outline: [],
    capabilities: {
      can_discuss: true,
      can_preview_revision: false,
      manual_edit: true,
      can_add_enrichment: true,
      can_preview_pdf: true,
      can_regenerate_pdf: false,
    },
    preview_status: "READY",
    impact_status: "none",
    pdf_status: "CURRENT",
    pdf_artifact_id: null,
    pdf_artifact_version: null,
    pdf_artifact_input_hash: null,
    pdf_artifact_canonical_sha256: null,
    pdf_render_id: null,
    pdf_render_identity: null,
    pdf_error_code: null,
    pdf_error_message: null,
    pdf_url: null,
    snapshot_id: "13131313-1313-4313-8313-131313131313",
  };
}

function createFigureProposal(
  input: EditorialManualSourceFigureProposalInput,
): EditorialProposalRead {
  return {
    id: proposalId,
    subject_id: subjectId,
    production_run_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    occurrence_id: anchorId,
    thread_id: null,
    origin: "manual_patch",
    state: "proposed",
    state_version: 1,
    operation: "create_source_figure",
    base_artifact_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
    base_artifact_version: 4,
    base_canonical_sha256: "a".repeat(64),
    base_text: null,
    patch_sha256: "d".repeat(64),
    patch: {
      schema_version: "editorial-patch-v1",
      operation: "create_source_figure",
      target: {},
      target_id: "manual-source-figures",
      occurrence_id: anchorId,
      before_hash: "e".repeat(64),
      rationale: "Images archivées sélectionnées manuellement.",
      enrichment_patch: {
        manual_source_figures: input.figures.map((selection) => {
          const item = catalog.find(
            (candidate) => candidate.figure_id === selection.figure_id,
          );
          if (!item) throw new Error("Missing synthetic catalog figure");
          return {
            ...selection,
            sha256: "c".repeat(64),
            mime_type: item.mime_type ?? "image/png",
            byte_size: 4096,
            width: item.width ?? 800,
            height: item.height ?? 450,
            source_document_id: item.source.source_id,
            source_url: item.source.url ?? "https://publisher.example/report",
            provenance:
              "Figure archivée et vérifiée dans la source synthétique.",
          };
        }),
      },
    },
    diagnostics: {},
    created_at: "2026-11-22T09:00:00Z",
  };
}

function revisionProposal(): EditorialVisualRevisionProposalRead {
  return {
    id: revisionProposalId,
    subject_id: subjectId,
    production_run_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    occurrence_id: figureBlockId,
    thread_id: null,
    origin: "manual_patch",
    state: "proposed",
    state_version: 1,
    operation: "replace_source_figure",
    base_artifact_id: "12121212-1212-4212-8212-121212121212",
    base_artifact_version: 2,
    base_canonical_sha256: "a".repeat(64),
    base_text: null,
    patch_sha256: "f".repeat(64),
    patch: null,
    diagnostics: {},
    created_at: "2026-11-22T09:00:00Z",
  };
}

interface Captures {
  figureProposals: EditorialManualSourceFigureProposalInput[];
  enrichmentRevisions: unknown[];
  accepts: unknown[];
  unexpected: string[];
}

async function fulfillCatalog(route: Route, url: URL): Promise<void> {
  const status = url.searchParams.get("status");
  const source = url.searchParams.get("source");
  const query = url.searchParams.get("q")?.toLowerCase() ?? "";
  let items = catalog;
  if (status) items = items.filter((item) => item.status === status);
  if (source) {
    items = items.filter((item) => item.source.source_id === source);
  }
  if (query) {
    items = items.filter((item) =>
      [item.original_name, item.original_caption, item.context_before]
        .filter((value): value is string => Boolean(value))
        .some((value) => value.toLowerCase().includes(query)),
    );
  }
  await route.fulfill({
    json: {
      items,
      sources: [catalog[0].source, catalog[2].source],
      total: items.length,
      available_count: items.filter((item) => item.selectable).length,
      offset: Number(url.searchParams.get("offset") ?? 0),
      limit: Number(url.searchParams.get("limit") ?? 30),
    },
  });
}

async function installRoutes(
  page: Page,
  options: {
    kind?: "article" | "brief";
    withIntegratedFigure?: boolean;
    withChart?: boolean;
  } = {},
): Promise<Captures> {
  const image = await readFile(
    new URL("./fixtures/figure-gallery-thumbnail.jpg", import.meta.url),
  );
  const captures: Captures = {
    figureProposals: [],
    enrichmentRevisions: [],
    accepts: [],
    unexpected: [],
  };
  const snapshot = createSnapshot(
    options.kind ?? "article",
    options.withIntegratedFigure ?? false,
    options.withChart ?? false,
  );
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== EDITORIAL_APP_ORIGIN) {
      captures.unexpected.push(`${method} ${url.origin}${url.pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    if (
      await fulfillEditorialReadStatuses(route, method, url.pathname, snapshot)
    ) {
      return;
    }
    if (method === "GET" && url.pathname === subjectPath) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
      return;
    }
    if (method === "GET" && url.pathname === `${subjectPath}/content`) {
      await route.fulfill({ status: 404, body: "" });
      return;
    }
    if (method === "GET" && url.pathname === snapshotPath) {
      await route.fulfill({ json: snapshot });
      return;
    }
    if (method === "GET" && url.pathname === figuresPath) {
      await fulfillCatalog(route, url);
      return;
    }
    if (
      method === "GET" &&
      url.pathname.startsWith(`${figuresPath}/`) &&
      url.pathname.endsWith("/thumbnail")
    ) {
      await route.fulfill({
        status: 200,
        contentType: "image/jpeg",
        body: image,
      });
      return;
    }
    const previewMatch = url.pathname.match(
      new RegExp(`^${figuresPath}/([^/]+)/preview$`),
    );
    if (method === "GET" && previewMatch) {
      const item = catalog.find(
        (candidate) => candidate.figure_id === previewMatch[1],
      );
      if (!item) {
        await route.fulfill({
          status: 404,
          json: { detail: { code: "editorial_figure_not_found" } },
        });
        return;
      }
      await route.fulfill({
        json: {
          ...item,
          preview_image_url: `${figuresPath}/${item.figure_id}/preview/image`,
        },
      });
      return;
    }
    if (
      method === "GET" &&
      url.pathname.startsWith(`${figuresPath}/`) &&
      url.pathname.endsWith("/preview/image")
    ) {
      await route.fulfill({
        status: 200,
        contentType: "image/jpeg",
        body: image,
      });
      return;
    }
    if (
      method === "GET" &&
      /\/editor\/blocks\/[^/]+\/draft$/.test(url.pathname)
    ) {
      await route.fulfill({
        status: 404,
        json: { detail: { code: "draft_not_found" } },
      });
      return;
    }
    const threadMatch = url.pathname.match(
      /^\/api\/subjects\/[^/]+\/editor\/threads\/([^/]+)$/,
    );
    if (method === "GET" && threadMatch) {
      const occurrenceId = threadMatch[1];
      const binding = {
        subject_id: subjectId,
        production_run_id: snapshot.production_run_id,
        occurrence_id: occurrenceId,
        artifact_id: snapshot.synthesis_artifact_id,
        artifact_version: snapshot.synthesis_version,
        canonical_sha256: snapshot.synthesis_canonical_sha256,
      };
      await route.fulfill({
        headers: { ETag: '"gallery-replace-thread"' },
        json: {
          kind: "unopened",
          binding,
          current: binding,
          bridge_status: "ready",
          event_sequence: 0,
          timeline: [],
          etag: '"gallery-replace-thread"',
        },
      });
      return;
    }
    if (
      method === "POST" &&
      url.pathname ===
        `${subjectPath}/editor/blocks/${anchorId}/figure-proposals`
    ) {
      const body = JSON.parse(
        route.request().postData() ?? "null",
      ) as EditorialManualSourceFigureProposalInput | null;
      if (!body) throw new Error("Missing synthetic figure proposal body");
      captures.figureProposals.push(body);
      await route.fulfill({ status: 201, json: createFigureProposal(body) });
      return;
    }
    if (
      method === "GET" &&
      url.pathname ===
        `${subjectPath}/editor/threads/${anchorId}/proposals/${proposalId}`
    ) {
      await route.fulfill({
        json: createFigureProposal({
          snapshot_id: snapshot.snapshot_id,
          anchor_block_id: anchorId,
          figures: captures.figureProposals[0]?.figures ?? [],
        }),
      });
      return;
    }
    if (
      method === "POST" &&
      url.pathname ===
        `${subjectPath}/editor/blocks/${figureBlockId}/enrichment-revisions`
    ) {
      captures.enrichmentRevisions.push(
        JSON.parse(route.request().postData() ?? "null") as unknown,
      );
      await route.fulfill({ status: 201, json: revisionProposal() });
      return;
    }
    if (
      method === "POST" &&
      url.pathname ===
        `${subjectPath}/editor/threads/${figureBlockId}/proposals/${revisionProposalId}/accept`
    ) {
      captures.accepts.push(
        JSON.parse(route.request().postData() ?? "null") as unknown,
      );
      await route.fulfill({
        json: {
          id: revisionProposalId,
          state: "accepted",
          state_version: 2,
          artifact_id: "14141414-1414-4414-8414-141414141414",
          artifact_version: 5,
          snapshot_id: "15151515-1515-4515-8515-151515151515",
          impact_id: "16161616-1616-4616-8616-161616161616",
          impact_state: "pending",
        },
      });
      return;
    }
    captures.unexpected.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });
  return captures;
}

async function selectAnchorAndOpenFigureGallery(page: Page): Promise<void> {
  const anchor = page.locator(
    `.editorial-document__block[data-block-id="${anchorId}"]`,
  );
  await anchor.click();
  await expect(anchor).toHaveAttribute("data-selected", "true");
  await page.getByRole("button", { name: "Ajouter après" }).click();
  await page.getByRole("menuitem", { name: "Image issue des sources" }).click();
}

async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const noOverflow = await page.evaluate(
    "document.scrollingElement ? document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth : true",
  );
  expect(noOverflow).toBe(true);
}

test.beforeAll(async () => {
  await mkdir(screenshotDirectory, { recursive: true });
});

test("E09 — galerie, filtre par source, aperçu sans perte de sélection et intégration ordonnée", async ({
  page,
}) => {
  const captures = await installRoutes(page, { kind: "article" });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await selectAnchorAndOpenFigureGallery(page);

  const gallery = page.getByRole("dialog", {
    name: "Galerie d’images sources",
  });
  await expect(gallery).toBeVisible();
  await gallery
    .getByRole("combobox", { name: "Article/source" })
    .selectOption({ label: "Rapport public source A" });
  await expect(
    gallery.getByRole("checkbox", {
      name: "Sélectionner Carte des flux réseau",
    }),
  ).toBeVisible();
  await expect(
    gallery.getByRole("checkbox", {
      name: "Sélectionner Schéma de l’infrastructure",
    }),
  ).toBeVisible();
  await expect(
    gallery.getByRole("checkbox", {
      name: "Sélectionner Photographie du serveur",
    }),
  ).toHaveCount(0);

  await gallery
    .getByRole("checkbox", { name: "Sélectionner Carte des flux réseau" })
    .check();
  await gallery
    .getByRole("checkbox", {
      name: "Sélectionner Schéma de l’infrastructure",
    })
    .check();
  await expect(gallery.getByText("2 images sélectionnées")).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "E09-galerie-selection-1440x1000.png"),
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "E09-galerie-selection-1280x900.png"),
  });

  await gallery
    .locator(".editorial-figure-card")
    .first()
    .getByRole("button", { name: "Aperçu détaillé" })
    .click();
  await expect(
    gallery.getByRole("heading", { name: "Aperçu détaillé" }),
  ).toBeVisible();
  await expect(
    gallery.getByText("Contexte précédent conservé dans l’archive."),
  ).toBeVisible();
  await expect(gallery.getByText("2 images sélectionnées")).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({
    path: resolve(screenshotDirectory, "E09-apercu-detail-1440x1000.png"),
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "E09-apercu-detail-1280x900.png"),
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await gallery.getByRole("button", { name: "Fermer l’aperçu" }).click();
  await gallery
    .getByRole("button", { name: "Fermer la galerie d’images sources" })
    .click();

  await page
    .getByRole("button", { name: "Monter Schéma de l’infrastructure" })
    .click();
  const insertionPanel = page.locator(".editorial-figure-insertion");
  expect(
    await insertionPanel.evaluate((element) => {
      const metrics = element as unknown as {
        scrollWidth?: number;
        clientWidth?: number;
      };
      return (metrics.scrollWidth ?? 0) - (metrics.clientWidth ?? 0);
    }),
  ).toBeLessThanOrEqual(0);
  await page.screenshot({
    path: resolve(screenshotDirectory, "E09-panneau-selection-1440x1000.png"),
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "E09-panneau-selection-1280x900.png"),
  });
  await page
    .getByRole("button", { name: "Intégrer les images sélectionnées" })
    .click();

  await expect(
    page.getByRole("heading", {
      name: "Comparaison de l’insertion de figures",
    }),
  ).toBeVisible();
  expect(captures.figureProposals).toHaveLength(1);
  expect(captures.figureProposals[0]).toEqual({
    snapshot_id: "13131313-1313-4313-8313-131313131313",
    anchor_block_id: anchorId,
    figures: [
      {
        figure_id: figureIds.a2,
        caption: "Légende archivée de Schéma de l’infrastructure",
        order: 0,
      },
      {
        figure_id: figureIds.a1,
        caption: "Légende archivée de Carte des flux réseau",
        order: 1,
      },
    ],
  });
  expect(captures.unexpected).toEqual([]);
});

test("E12 — remplacement comparé, confirmé par une seule révision typée", async ({
  page,
}) => {
  const captures = await installRoutes(page, {
    kind: "article",
    withIntegratedFigure: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  const figureBlock = page.locator(
    `.editorial-document__block[data-block-id="${figureBlockId}"]`,
  );
  await figureBlock.click();
  await expect(figureBlock).toHaveAttribute("data-selected", "true");
  await page.getByRole("tab", { name: "Visuels" }).click();
  await expect(
    page.getByRole("list", { name: "Éléments visuels du document" }),
  ).toBeVisible();
  await page.screenshot({
    path: resolve(screenshotDirectory, "E12-onglet-visuels-1440x1000.png"),
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({
    path: resolve(screenshotDirectory, "E12-onglet-visuels-1280x900.png"),
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  // The selected figure also shows its document action bar; this scenario is
  // the inspector-list path, so the request must come from the Visuels tab.
  await page
    .getByLabel("Inspecteur éditorial")
    .getByRole("button", { name: "Remplacer" })
    .click();

  const dialog = page.getByRole("dialog", {
    name: "Remplacer l’image intégrée",
  });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText(figureCaption)).toBeVisible();
  await dialog
    .getByRole("radio", { name: "Choisir Carte des flux réseau" })
    .check();
  const comparison = dialog.getByRole("region", {
    name: "Comparaison des images",
  });
  await expect(
    comparison.getByRole("heading", { name: "Image actuelle" }),
  ).toBeVisible();
  await expect(
    comparison.getByRole("heading", { name: "Nouvelle image" }),
  ).toBeVisible();
  await expect(comparison).toContainText(
    "Légende archivée de Carte des flux réseau",
  );
  await expect(comparison).toContainText(
    `Emplacement conservé · après « ${anchorText} »`,
  );
  await expect(comparison).toContainText(
    "L’archive d’origine n’est pas modifiée",
  );
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "E12-comparaison-1440x1000.png"),
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "E12-comparaison-1280x900.png"),
  });

  await dialog.getByRole("button", { name: "Remplacer l’image" }).click();
  await expect(
    dialog.getByText(
      "Image remplacée. Le document contient toujours exactement une image à cet emplacement.",
    ),
  ).toBeVisible();
  await page.screenshot({
    path: resolve(
      screenshotDirectory,
      "E12-remplacement-confirme-1280x900.png",
    ),
  });

  expect(captures.enrichmentRevisions).toEqual([
    {
      snapshot_id: "13131313-1313-4313-8313-131313131313",
      operation: "replace_source_figure",
      element_key: elementKey,
      figure_id: figureIds.a1,
    },
  ]);
  expect(captures.accepts).toEqual([
    { expected_state_version: 1, patch_sha256: "f".repeat(64) },
  ]);
  expect(captures.figureProposals).toEqual([]);
  expect(captures.unexpected).toEqual([]);
});

test("E15 — plafond BRIEF expliqué et quatrième sélection bloquée", async ({
  page,
}) => {
  await installRoutes(page, { kind: "brief" });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await selectAnchorAndOpenFigureGallery(page);

  const gallery = page.getByRole("dialog", {
    name: "Galerie d’images sources",
  });
  await expect(
    gallery.getByText(
      /Capacité du document : 0\/3 figures intégrées · 3 places restantes/,
    ),
  ).toBeVisible();
  for (const name of [
    "Carte des flux réseau",
    "Schéma de l’infrastructure",
    "Photographie du serveur",
  ]) {
    await gallery
      .getByRole("checkbox", { name: `Sélectionner ${name}` })
      .check();
  }
  await expect(gallery.getByText("3 images sélectionnées")).toBeVisible();
  await expect(
    gallery.getByRole("checkbox", {
      name: "Sélectionner Capture de configuration",
    }),
  ).toBeDisabled();
  await expect(
    gallery.getByText(/Sélection : 3\/3 — le plafond de sélection est atteint/),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "E15-plafond-brief-1440x1000.png"),
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.screenshot({
    path: resolve(screenshotDirectory, "E15-plafond-brief-1280x900.png"),
  });
});

test("E15 — ARTICLE sans plafond de figures", async ({ page }) => {
  await installRoutes(page, { kind: "article" });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await selectAnchorAndOpenFigureGallery(page);

  const gallery = page.getByRole("dialog", {
    name: "Galerie d’images sources",
  });
  await expect(gallery.getByText(/Capacité du document/)).toHaveCount(0);
  const fourth = gallery.getByRole("checkbox", {
    name: "Sélectionner Capture de configuration",
  });
  await expect(fourth).toBeEnabled();
  for (const name of [
    "Carte des flux réseau",
    "Schéma de l’infrastructure",
    "Photographie du serveur",
    "Capture de configuration",
  ]) {
    await gallery
      .getByRole("checkbox", { name: `Sélectionner ${name}` })
      .check();
  }
  await expect(gallery.getByText("4 images sélectionnées")).toBeVisible();
  await page.screenshot({
    path: resolve(
      screenshotDirectory,
      "E15-article-sans-plafond-1440x1000.png",
    ),
  });
  await page.setViewportSize({ width: 1280, height: 900 });
  await expectNoHorizontalOverflow(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "E15-article-sans-plafond-1280x900.png"),
  });
});

test("E19 — sélection de galerie et remplacement au clavier, aperçu sans perte de sélection", async ({
  page,
}) => {
  const captures = await installRoutes(page, {
    kind: "article",
    withIntegratedFigure: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const figureBlock = page.locator(
    `.editorial-document__block[data-block-id="${figureBlockId}"]`,
  );
  await figureBlock.focus();
  await page.keyboard.press("Enter");
  await expect(figureBlock).toHaveAttribute("data-selected", "true");
  await page.getByRole("tab", { name: "Visuels" }).focus();
  await page.keyboard.press("Enter");
  await page
    .getByLabel("Inspecteur éditorial")
    .getByRole("button", { name: "Remplacer" })
    .focus();
  await page.keyboard.press("Enter");

  const dialog = page.getByRole("dialog", {
    name: "Remplacer l’image intégrée",
  });
  await expect(
    dialog.getByRole("radio", { name: "Choisir Capture non admissible" }),
  ).toBeDisabled();
  await expect(
    dialog.getByText(
      "La licence de cette figure ne permet pas son intégration.",
    ),
  ).toBeVisible();
  const radio = dialog.getByRole("radio", {
    name: "Choisir Schéma de l’infrastructure",
  });
  await radio.focus();
  await page.keyboard.press("Space");
  await expect(radio).toBeChecked();
  await dialog.getByRole("button", { name: "Aperçu détaillé" }).first().focus();
  await page.keyboard.press("Enter");
  await expect(
    dialog.getByRole("heading", { name: "Aperçu détaillé" }),
  ).toBeVisible();
  await expect(radio).toBeChecked();
  await dialog.getByRole("button", { name: "Fermer l’aperçu" }).focus();
  await page.keyboard.press("Enter");

  await dialog.getByRole("button", { name: "Remplacer l’image" }).focus();
  await page.keyboard.press("Enter");
  await expect(
    dialog.getByText(
      "Image remplacée. Le document contient toujours exactement une image à cet emplacement.",
    ),
  ).toBeVisible();
  await page.screenshot({
    path: resolve(
      screenshotDirectory,
      "E19-clavier-remplacement-1440x1000.png",
    ),
  });
  expect(captures.enrichmentRevisions).toHaveLength(1);
  expect(captures.enrichmentRevisions[0]).toMatchObject({
    operation: "replace_source_figure",
    element_key: elementKey,
    figure_id: figureIds.a2,
  });
  expect(captures.unexpected).toEqual([]);
});

const f3cScreenshotDirectory = resolve(
  process.cwd(),
  "../.autowork-runs/shots-f3c",
);

test("E20 — déplacement depuis l’onglet Visuels: panneau inspecteur, ancrage et confirmation", async ({
  page,
}) => {
  const captures = await installRoutes(page, {
    kind: "article",
    withIntegratedFigure: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  const figureBlock = page.locator(
    `.editorial-document__block[data-block-id="${figureBlockId}"]`,
  );
  await figureBlock.click();
  await page.getByRole("tab", { name: "Visuels" }).click();

  const figureItem = page.locator(
    '.editorial-visuals__item[data-kind="figure"]',
  );
  await figureItem.getByRole("button", { name: "Déplacer" }).click();
  const panel = page.getByRole("region", { name: "Déplacer le visuel" });
  await expect(panel).toBeVisible();
  await expect(panel.getByText(/Emplacement actuel : après «/)).toBeVisible();

  const anchorChoice = panel.getByRole("radio", {
    name: /Paragraphe suivant conservé/,
  });
  await anchorChoice.focus();
  await page.keyboard.press("Space");
  await expect(anchorChoice).toBeChecked();
  await expect(panel.getByText(/Emplacement prévu : après «/)).toBeVisible();

  const confirm = panel.getByRole("button", {
    name: "Enregistrer l’emplacement",
  });
  await expect(confirm).toBeEnabled();
  await confirm.focus();
  await page.keyboard.press("Enter");

  await expect
    .poll(() => captures.enrichmentRevisions.length)
    .toBeGreaterThan(0);
  expect(captures.enrichmentRevisions.at(-1)).toMatchObject({
    snapshot_id: "13131313-1313-4313-8313-131313131313",
    operation: "move_enrichment_element",
    element_kind: "figure",
    element_key: elementKey,
    anchor_block_id: secondParagraphId,
  });
  await expect(page.getByText(/Emplacement enregistré · v\d+/)).toBeVisible();
  expect(captures.unexpected).toEqual([]);
});

test("E21 — fermer le remplacement ou le déplacement rend le focus au contrôle d’origine", async ({
  page,
}) => {
  await installRoutes(page, {
    kind: "article",
    withIntegratedFigure: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await page
    .locator(`.editorial-document__block[data-block-id="${figureBlockId}"]`)
    .click();
  await page.getByRole("tab", { name: "Visuels" }).click();
  const figureItem = page.locator(
    '.editorial-visuals__item[data-kind="figure"]',
  );

  const replaceButton = figureItem.getByRole("button", { name: "Remplacer" });
  await replaceButton.click();
  const dialog = page.getByRole("dialog", {
    name: "Remplacer l’image intégrée",
  });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(replaceButton).toBeFocused();

  const moveButton = figureItem.getByRole("button", { name: "Déplacer" });
  await moveButton.click();
  const panel = page.getByRole("region", { name: "Déplacer le visuel" });
  await expect(panel).toBeVisible();
  await panel.getByRole("button", { name: "Annuler" }).click();
  await expect(panel).toHaveCount(0);
  await expect(moveButton).toBeFocused();
});

test("F3-C — captures desktop de l’atelier connecté (1024, 1280, 1440, 1920)", async ({
  page,
}) => {
  await installRoutes(page, {
    kind: "article",
    withIntegratedFigure: true,
    withChart: true,
  });
  await mkdir(f3cScreenshotDirectory, { recursive: true });
  const figureBlock = page.locator(
    `.editorial-document__block[data-block-id="${figureBlockId}"]`,
  );
  const inspector = page.getByLabel("Inspecteur éditorial");
  const sizes = [
    [1024, 900],
    [1280, 900],
    [1440, 1000],
    [1920, 1080],
  ] as const;

  for (const [width, height] of sizes) {
    await page.setViewportSize({ width, height });
    await page.goto(`/subjects/${subjectId}/editor`);
    await figureBlock.click();
    await expect(figureBlock).toHaveAttribute("data-selected", "true");
    await page.getByRole("tab", { name: "Visuels" }).click();
    await expect(
      page.getByRole("list", { name: "Éléments visuels du document" }),
    ).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({
      path: resolve(
        f3cScreenshotDirectory,
        `visuels-liste-${width}x${height}.png`,
      ),
    });

    await page
      .getByRole("button", {
        name: "Ouvrir la galerie des images archivées",
      })
      .click();
    const gallery = page.getByRole("dialog", {
      name: "Galerie d’images sources",
    });
    await expect(gallery).toBeVisible();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({
      path: resolve(
        f3cScreenshotDirectory,
        `visuels-galerie-${width}x${height}.png`,
      ),
    });
    await gallery
      .getByRole("button", { name: "Fermer la galerie d’images sources" })
      .click();

    await inspector.getByRole("button", { name: "Remplacer" }).click();
    const replaceDialog = page.getByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    await expect(replaceDialog).toBeVisible();
    await replaceDialog
      .getByRole("radio", { name: "Choisir Carte des flux réseau" })
      .check();
    await expect(
      replaceDialog.getByRole("region", { name: "Comparaison des images" }),
    ).toContainText("Emplacement conservé");
    await replaceDialog
      .getByRole("region", { name: "Comparaison des images" })
      .scrollIntoViewIfNeeded();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({
      path: resolve(
        f3cScreenshotDirectory,
        `remplacement-comparaison-${width}x${height}.png`,
      ),
    });
    await replaceDialog
      .getByRole("button", { name: "Fermer le remplacement d’image" })
      .click();
    // A chosen replacement makes the dialog dirty: the close is confirmed.
    await replaceDialog
      .getByRole("button", { name: "Fermer sans enregistrer" })
      .click();
    await expect(replaceDialog).toHaveCount(0);

    const figureItem = page.locator(
      '.editorial-visuals__item[data-kind="figure"]',
    );
    await figureItem.getByRole("button", { name: "Déplacer" }).click();
    const movePanel = page.getByRole("region", {
      name: "Déplacer le visuel",
    });
    await expect(movePanel).toBeVisible();
    await movePanel.scrollIntoViewIfNeeded();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({
      path: resolve(
        f3cScreenshotDirectory,
        `panneau-deplacement-${width}x${height}.png`,
      ),
    });
    await movePanel.getByRole("button", { name: "Annuler" }).click();
    await expect(movePanel).toHaveCount(0);

    const chartBlock = page.locator(
      `.editorial-document__block[data-element-key="${chartKey}"]`,
    );
    await chartBlock.click();
    await expect(chartBlock).toHaveAttribute("data-selected", "true");
    const chartActions = page.getByRole("toolbar", {
      name: "Actions du visuel : Graphique des volumes observés",
    });
    await expect(chartActions).toBeVisible();
    await chartActions.scrollIntoViewIfNeeded();
    await expectNoHorizontalOverflow(page);
    await page.screenshot({
      path: resolve(
        f3cScreenshotDirectory,
        `document-graphique-${width}x${height}.png`,
      ),
    });
  }
});
