import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import type {
  EditorialFigureCatalogItem,
  EditorialFigurePreview,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const appOrigin = EDITORIAL_APP_ORIGIN;
const paragraphId = "22222222-2222-4222-8222-222222222222";
const figureIds = [
  "11111111-1111-4111-8111-111111111111",
  "22222222-2222-4222-8222-222222222222",
  "33333333-3333-4333-8333-333333333333",
  "44444444-4444-4444-8444-444444444444",
  "55555555-5555-4555-8555-555555555555",
  "66666666-6666-4666-8666-666666666666",
];
const sourceId = "77777777-7777-4777-8777-777777777777";
const baseItem = (
  index: number,
  status: EditorialFigureCatalogItem["status"],
): EditorialFigureCatalogItem => ({
  figure_id: figureIds[index],
  original_name: [
    "Carte des flux réseau",
    "Schéma de l’infrastructure",
    "Photographie du serveur",
    "Capture de configuration",
    "Graphique non retenu",
    "Figure sans validation",
  ][index],
  original_caption: [
    "Vue synthétique des échanges observés.",
    "Légende issue de la source archivée.",
    "Illustration conservée dans l’archive.",
    "Contexte de la capture conservé.",
    "Contenu explicitement écarté.",
    "En attente de vérification.",
  ][index],
  alt_text: `Figure synthétique ${index + 1}`,
  source: {
    source_id: sourceId,
    title: "Rapport public synthétique",
    url: "https://publisher.example/report",
  },
  page: index + 1,
  section: "Infrastructure",
  context_before: "Contexte précédent conservé depuis la source.",
  context_after: "Contexte suivant conservé depuis la source.",
  width: 1280,
  height: 720,
  mime_type: "image/png",
  status,
  selectable: status === "available",
  exclusion_reason:
    status === "ineligible"
      ? "La licence de cette figure ne permet pas son intégration."
      : status === "unverified"
        ? "La figure n’a pas encore été vérifiée."
        : null,
  already_used: status === "already_integrated",
});

const items: EditorialFigureCatalogItem[] = [
  baseItem(0, "available"),
  baseItem(1, "available"),
  baseItem(2, "already_integrated"),
  baseItem(3, "ineligible"),
  baseItem(4, "not_selected"),
  baseItem(5, "unverified"),
];

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: subjectId,
  production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  publication_kind: "article",
  synthesis_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  synthesis_version: 4,
  synthesis_canonical_sha256: "a".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  publication_version: 3,
  pipeline_generation: 2,
  blocks: [
    {
      block_id: paragraphId,
      kind: "paragraph",
      parent_block_id: null,
      position: 0,
      text: "Paragraphe synthétique utilisé pour ouvrir l’inspecteur.",
      evidence_refs: [
        {
          source_title: "Rapport public synthétique",
          passage: "Preuve synthétique.",
        },
      ],
      chart: null,
      revision_target: { block_id: paragraphId, legacy_target_id: null },
      before_hash: null,
      allowed_operations: ["rewrite_paragraph", "insert_paragraph"],
      revision_status: "ready",
    },
  ],
  enrichments: [],
  document_outline: [],
  capabilities: {
    can_discuss: false,
    can_preview_revision: false,
    manual_edit: false,
    can_add_enrichment: false,
    can_preview_pdf: true,
  },
  preview_status: "READY",
  impact_status: "none",
  pdf_status: "CURRENT",
  snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
};

async function stubEditorApi(page: Page): Promise<string[]> {
  const unexpectedRequests: string[] = [];
  const figureThumbnail = await readFile(
    new URL("./fixtures/figure-gallery-thumbnail.jpg", import.meta.url),
  );
  const subjectPath = `/api/subjects/${subjectId}`;
  const figuresPath = `${subjectPath}/editor/figures`;
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== appOrigin) {
      unexpectedRequests.push(`${method} ${url.origin}${url.pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
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
    if (method === "GET" && url.pathname === `${subjectPath}/editor/snapshot`) {
      await route.fulfill({ json: snapshot });
      return;
    }
    if (method === "GET" && url.pathname === figuresPath) {
      await route.fulfill({
        json: {
          items,
          sources: [
            {
              source_id: sourceId,
              title: "Rapport public synthétique",
              url: "https://publisher.example/report",
            },
          ],
          total: items.length,
          available_count: 2,
          offset: Number(url.searchParams.get("offset") ?? 0),
          limit: Number(url.searchParams.get("limit") ?? 30),
        },
      });
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
        body: figureThumbnail,
      });
      return;
    }
    const previewMatch = url.pathname.match(
      new RegExp(`^${figuresPath}/([^/]+)/preview$`),
    );
    if (method === "GET" && previewMatch) {
      const item = items.find(
        (candidate) => candidate.figure_id === previewMatch[1],
      );
      if (!item) {
        await route.fulfill({
          status: 404,
          json: { detail: { code: "editorial_figure_not_found" } },
        });
        return;
      }
      const preview: EditorialFigurePreview = {
        ...item,
        preview_image_url: `${figuresPath}/${item.figure_id}/preview/image`,
      };
      await route.fulfill({ json: preview });
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
        body: figureThumbnail,
      });
      return;
    }
    const threadPrefix = `${subjectPath}/editor/threads/`;
    if (method === "GET" && url.pathname.startsWith(threadPrefix)) {
      const occurrenceId = url.pathname.slice(threadPrefix.length);
      const binding = {
        subject_id: subjectId,
        production_run_id: snapshot.production_run_id,
        occurrence_id: occurrenceId,
        artifact_id: snapshot.synthesis_artifact_id,
        artifact_version: snapshot.synthesis_version,
        canonical_sha256: snapshot.synthesis_canonical_sha256,
      };
      const etag = '"figure-gallery-thread-empty"';
      if (route.request().headers()["if-none-match"] === etag) {
        await route.fulfill({ status: 304, headers: { ETag: etag } });
        return;
      }
      await route.fulfill({
        status: 200,
        headers: { ETag: etag },
        json: {
          kind: "unopened",
          binding,
          current: binding,
          bridge_status: "ready",
          event_sequence: 0,
          timeline: [],
          etag,
        },
      });
      return;
    }
    if (
      method === "GET" &&
      /^\/api\/subjects\/[^/]+\/editor\/blocks\/[^/]+\/draft$/.test(
        url.pathname,
      )
    ) {
      await route.fulfill({
        status: 404,
        json: { detail: { code: "draft_not_found" } },
      });
      return;
    }
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });
  return unexpectedRequests;
}

test("galerie figures: états bornés, sélection locale et aperçu détaillé à 1440 px", async ({
  page,
}) => {
  const unexpectedRequests = await stubEditorApi(page);
  const screenshotDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-T-M6-24",
  );
  await mkdir(screenshotDirectory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const paragraph = page.locator(".editorial-document__text");
  await expect(paragraph).toBeVisible();
  await expect(paragraph).toHaveText(
    "Paragraphe synthétique utilisé pour ouvrir l’inspecteur.",
  );
  await paragraph.click();
  await page.getByRole("button", { name: "Images sources" }).click();

  const gallery = page.getByRole("dialog", {
    name: "Galerie d’images sources",
  });
  await expect(gallery).toBeVisible();
  await expect(gallery.getByText("Disponibles : 2")).toBeVisible();
  const gridColumns = await gallery
    .locator(".editorial-figure-gallery__grid")
    .evaluate((grid) => {
      const styledGrid = grid as unknown as {
        ownerDocument: {
          defaultView: {
            getComputedStyle: (element: unknown) => {
              gridTemplateColumns: string;
            };
          } | null;
        };
      };
      const columns =
        styledGrid.ownerDocument.defaultView?.getComputedStyle(styledGrid)
          .gridTemplateColumns ?? "";
      return columns.trim().split(/\s+/).filter(Boolean).length;
    });
  expect(gridColumns).toBeGreaterThanOrEqual(2);
  expect(gridColumns).toBeLessThanOrEqual(4);
  const statusBadges = gallery.locator(".editorial-figure-status");
  for (const statusLabel of [
    "Disponible",
    "Déjà intégrée",
    "Non éligible",
    "Non retenue",
    "Figure non vérifiée",
  ]) {
    await expect(
      statusBadges.filter({ hasText: statusLabel }).first(),
    ).toBeVisible();
  }
  await expect(
    gallery.getByRole("checkbox", {
      name: "Sélectionner Capture de configuration",
    }),
  ).toBeDisabled();
  await expect(
    gallery.getByRole("button", { name: "Intégrer les images sélectionnées" }),
  ).toHaveCount(0);
  await expect(
    gallery.locator(".editorial-figure-card img").first(),
  ).toHaveAttribute("loading", "lazy");

  await gallery
    .getByRole("checkbox", { name: "Sélectionner Carte des flux réseau" })
    .check();
  await expect(gallery.getByText("1 image sélectionnée")).toBeVisible();
  await page.screenshot({
    path: resolve(screenshotDirectory, "galerie-1440x1000.png"),
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
    gallery.getByText("Contexte précédent conservé depuis la source."),
  ).toBeVisible();
  await expect(gallery.getByText("Page 1 · Infrastructure")).toBeVisible();
  await expect(gallery.getByText("1 image sélectionnée")).toBeVisible();
  await page.screenshot({
    path: resolve(screenshotDirectory, "apercu-detaille-1440x1000.png"),
  });
  await gallery.getByRole("button", { name: "Fermer l’aperçu" }).click();
  await gallery
    .locator(".editorial-figure-gallery__layout")
    .evaluate((layout) => {
      const scrollable = layout as unknown as {
        scrollTop: number;
        scrollHeight: number;
      };
      scrollable.scrollTop = scrollable.scrollHeight;
    });
  await expect(
    gallery.getByText(
      "La licence de cette figure ne permet pas son intégration.",
    ),
  ).toBeVisible();
  await page.screenshot({
    path: resolve(screenshotDirectory, "galerie-etats-exclus-1440x1000.png"),
  });
  expect(unexpectedRequests).toEqual([]);
});
