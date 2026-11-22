import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import type {
  EditorialFigureCatalogItem,
  EditorialManualSourceFigureProposalInput,
  EditorialProposalRead,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
  fulfillEditorialReadStatuses,
} from "./fixtures/editorialApi";

interface ScrollBounds {
  getBoundingClientRect(): { top: number };
}

interface ScrollableBounds extends ScrollBounds {
  scrollTop: number;
}

const subjectId = EDITORIAL_FIXTURES.subject.id;
const paragraphIds = [
  "11111111-1111-4111-8111-111111111111",
  "22222222-2222-4222-8222-222222222222",
  "33333333-3333-4333-8333-333333333333",
];
const figureIds = [
  "44444444-4444-4444-8444-444444444444",
  "55555555-5555-4555-8555-555555555555",
  "66666666-6666-4666-8666-666666666666",
];
const internalFigureIds = [
  "99999999-9999-4999-8999-999999999999",
  "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
];
const sourceId = "77777777-7777-4777-8777-777777777777";
const proposalId = "88888888-8888-4888-8888-888888888888";
const anchorId = paragraphIds[1];
const subjectPath = `/api/subjects/${subjectId}`;
const snapshotPath = `${subjectPath}/editor/snapshot`;
const figuresPath = `${subjectPath}/editor/figures`;
const proposalPath = `${subjectPath}/editor/threads/${anchorId}/proposals/${proposalId}`;
const figureProposalPath = `${subjectPath}/editor/blocks/${anchorId}/figure-proposals`;
const acceptPath = `${proposalPath}/accept`;

const catalogItems: EditorialFigureCatalogItem[] = [
  {
    figure_id: figureIds[0],
    original_name: "Carte des échanges",
    original_caption: "Vue des connexions consignées dans la source.",
    alt_text: "Carte synthétique des échanges",
    source: {
      source_id: sourceId,
      title: "Rapport public synthétique",
      url: "https://publisher.example/report",
    },
    page: 4,
    section: "Infrastructure",
    context_before: "Le contexte précédent est synthétique.",
    context_after: "Le contexte suivant est synthétique.",
    width: 1280,
    height: 720,
    mime_type: "image/png",
    status: "available",
    selectable: true,
    exclusion_reason: null,
    already_used: false,
  },
  {
    figure_id: figureIds[1],
    original_name: "Schéma de l’infrastructure",
    original_caption: "Légende de la seconde image archivée.",
    alt_text: "Schéma synthétique de l’infrastructure",
    source: {
      source_id: sourceId,
      title: "Rapport public synthétique",
      url: "https://publisher.example/report",
    },
    page: 5,
    section: "Infrastructure",
    context_before: null,
    context_after: null,
    width: 800,
    height: 450,
    mime_type: "image/png",
    status: "available",
    selectable: true,
    exclusion_reason: null,
    already_used: false,
  },
  {
    figure_id: figureIds[2],
    original_name: "Capture non admissible",
    original_caption: null,
    alt_text: null,
    source: {
      source_id: sourceId,
      title: "Rapport public synthétique",
      url: "https://publisher.example/report",
    },
    page: 6,
    section: "Annexe",
    context_before: null,
    context_after: null,
    width: 1200,
    height: 900,
    mime_type: "image/png",
    status: "ineligible",
    selectable: false,
    exclusion_reason:
      "La licence de cette figure ne permet pas son intégration.",
    already_used: false,
  },
];

function paragraphBlock(index: number): EditorialWorkspaceBlock {
  const blockId = paragraphIds[index];
  return {
    block_id: blockId,
    kind: "paragraph",
    parent_block_id: null,
    position: index,
    text: [
      "Premier paragraphe synthétique.",
      "Paragraphe courant où les images seront ajoutées.",
      "Paragraphe suivant, conservé après l’insertion.",
    ][index],
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: "a".repeat(64),
    allowed_operations:
      index === 1 ? ["insert_after", "create_source_figure"] : [],
    revision_status: "ready",
  };
}

function createSnapshot(
  kind: "article" | "brief",
  existingFigures: number,
  insertedFigures: EditorialManualSourceFigureProposalInput["figures"] = [],
): EditorialWorkspaceSnapshot {
  const paragraphs = paragraphIds.map((_, index) => paragraphBlock(index));
  const alreadyPresent: EditorialWorkspaceBlock[] = Array.from(
    { length: existingFigures },
    (_, index) => {
      const figureId = figureIds[index % figureIds.length];
      const internalFigureId =
        internalFigureIds[index % internalFigureIds.length];
      if (!figureId || !internalFigureId) {
        throw new Error("Missing synthetic figure identity");
      }
      return {
        ...paragraphs[1],
        block_id: `existing-figure-${index}`,
        kind: "figure",
        position: index + 1,
        text: `Figure déjà intégrée ${index + 1}`,
        allowed_operations: [],
        figure: {
          key: `adopted_source_figure_${internalFigureId.replaceAll("-", "")}`,
          catalog_figure_id: figureId,
          caption: `Figure déjà intégrée ${index + 1}`,
          source_url: "https://publisher.example/report",
          source: "Rapport public synthétique",
          provenance: "Figure archivée et vérifiée.",
          sha256: "b".repeat(64),
          mime_type: "image/png",
          byte_size: 4096,
          width: 1280,
          height: 720,
          status: "included",
          order: index,
          anchor_block_id: anchorId,
          placement: "after_paragraph",
          page: 4,
          section: "Infrastructure",
        },
      };
    },
  );
  const inserted: EditorialWorkspaceBlock[] = insertedFigures.map(
    (selection, index) => {
      const internalFigureId =
        internalFigureIds[index % internalFigureIds.length];
      const catalogItem = catalogItems.find(
        (item) => item.figure_id === selection.figure_id,
      );
      if (!catalogItem || !internalFigureId) {
        throw new Error("Missing synthetic figure identity");
      }
      return {
        ...paragraphs[1],
        block_id: `inserted-figure-${index + 1}`,
        kind: "figure",
        position: index + 1,
        text: selection.caption,
        allowed_operations: [],
        figure: {
          key: `adopted_source_figure_${internalFigureId.replaceAll("-", "")}`,
          catalog_figure_id: selection.figure_id,
          caption: selection.caption,
          source_url: catalogItem.source.url ?? "",
          source: catalogItem.source.title,
          provenance: "Figure archivée et vérifiée dans la source synthétique.",
          sha256: (index === 0 ? "c" : "d").repeat(64),
          mime_type: catalogItem.mime_type ?? "image/png",
          byte_size: 4096,
          width: catalogItem.width,
          height: catalogItem.height,
          status: "included",
          order: selection.order,
          anchor_block_id: anchorId,
          placement: "after_paragraph",
          page: catalogItem.page,
          section: catalogItem.section,
        },
      };
    },
  );
  const blocks = [
    ...paragraphs.slice(0, 2),
    ...alreadyPresent,
    ...inserted,
    paragraphs[2],
  ];
  blocks.forEach((block, index) => {
    block.position = index;
  });
  return {
    subject_id: subjectId,
    production_run_id: "99999999-9999-4999-8999-999999999999",
    publication_kind: kind,
    synthesis_artifact_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    synthesis_version: inserted.length > 0 ? 2 : 1,
    synthesis_canonical_sha256:
      inserted.length > 0 ? "c".repeat(64) : "a".repeat(64),
    enrichment_artifact_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    enrichment_version: inserted.length > 0 ? 2 : 1,
    publication_artifact_id: null,
    publication_version: null,
    pipeline_generation: 1,
    blocks,
    enrichments: [...alreadyPresent, ...inserted],
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
    impact_status: inserted.length > 0 ? "pending" : "none",
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
    snapshot_id:
      inserted.length > 0
        ? "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        : "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  };
}

function createProposal(
  input: EditorialManualSourceFigureProposalInput,
): EditorialProposalRead {
  return {
    id: proposalId,
    subject_id: subjectId,
    production_run_id: "99999999-9999-4999-8999-999999999999",
    occurrence_id: anchorId,
    thread_id: null,
    origin: "manual_patch",
    state: "proposed",
    state_version: 1,
    operation: "create_source_figure",
    base_artifact_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    base_artifact_version: 1,
    base_canonical_sha256: "a".repeat(64),
    base_text: null,
    patch_sha256: "e".repeat(64),
    patch: {
      schema_version: "editorial-patch-v1",
      operation: "create_source_figure",
      target: {},
      target_id: "manual-source-figures",
      occurrence_id: anchorId,
      before_hash: "f".repeat(64),
      rationale: "Images archivées sélectionnées manuellement.",
      enrichment_patch: {
        manual_source_figures: input.figures.map((selection) => {
          const item = catalogItems.find(
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
            source_document_id: sourceId,
            source_url: item.source.url ?? "https://publisher.example/report",
            provenance:
              "Figure archivée et vérifiée dans la source synthétique.",
          };
        }),
      },
    },
    diagnostics: {},
    created_at: "2026-11-21T08:00:00Z",
  };
}

async function installRoutes(
  page: Page,
  options: {
    kind?: "article" | "brief";
    existingFigures?: number;
    refuseBriefLimit?: boolean;
  } = {},
) {
  const image = await readFile(
    new URL("./fixtures/figure-gallery-thumbnail.jpg", import.meta.url),
  );
  const unexpectedRequests: string[] = [];
  const proposalRequests: EditorialManualSourceFigureProposalInput[] = [];
  let snapshot = createSnapshot(
    options.kind ?? "article",
    options.existingFigures ?? 0,
  );
  let proposal = createProposal({
    snapshot_id: snapshot.snapshot_id,
    anchor_block_id: anchorId,
    figures: [],
  });
  let refusalSent = false;
  let proposalInput: EditorialManualSourceFigureProposalInput | null = null;

  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== EDITORIAL_APP_ORIGIN) {
      unexpectedRequests.push(`${method} ${url.origin}${url.pathname}`);
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
    if (method === "GET" && url.pathname === `${subjectPath}`) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
      return;
    }
    if (method === "GET" && url.pathname === `${subjectPath}/content`) {
      await route.fulfill({ status: 404, body: "" });
      return;
    }
    if (method === "GET" && url.pathname === snapshotPath) {
      if (refusalSent) snapshot = createSnapshot("brief", 3);
      await route.fulfill({ json: snapshot });
      return;
    }
    if (method === "GET" && url.pathname === figuresPath) {
      await route.fulfill({
        json: {
          items: catalogItems,
          sources: [catalogItems[0].source],
          total: catalogItems.length,
          available_count: catalogItems.filter((item) => item.selectable)
            .length,
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
      const figureId = url.pathname.slice(
        figuresPath.length + 1,
        -"/thumbnail".length,
      );
      if (!catalogItems.some((item) => item.figure_id === figureId)) {
        unexpectedRequests.push(`${method} ${url.pathname}`);
        await route.fulfill({
          status: 404,
          json: { detail: { code: "editorial_figure_not_found" } },
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "image/jpeg",
        body: image,
      });
      return;
    }
    if (
      method === "GET" &&
      new RegExp(`/api/subjects/${subjectId}/editor/blocks/[^/]+/draft$`).test(
        url.pathname,
      )
    ) {
      await route.fulfill({
        status: 404,
        json: { detail: { code: "draft_not_found" } },
      });
      return;
    }
    const threadPath = `${subjectPath}/editor/threads/${anchorId}`;
    if (method === "GET" && url.pathname === threadPath) {
      const binding = {
        subject_id: subjectId,
        production_run_id: snapshot.production_run_id,
        occurrence_id: anchorId,
        artifact_id: snapshot.synthesis_artifact_id,
        artifact_version: snapshot.synthesis_version,
        canonical_sha256: snapshot.synthesis_canonical_sha256,
      };
      await route.fulfill({
        headers: { ETag: '"figure-insertion-thread"' },
        json: {
          kind: "unopened",
          binding,
          current: binding,
          bridge_status: "ready",
          event_sequence: 0,
          timeline: [],
          etag: '"figure-insertion-thread"',
        },
      });
      return;
    }
    if (method === "POST" && url.pathname === figureProposalPath) {
      const body = JSON.parse(
        route.request().postData() ?? "null",
      ) as EditorialManualSourceFigureProposalInput | null;
      if (!body) throw new Error("Missing synthetic figure proposal body");
      proposalInput = body;
      proposalRequests.push(body);
      if (options.refuseBriefLimit && snapshot.publication_kind === "brief") {
        refusalSent = true;
        await route.fulfill({
          status: 409,
          json: {
            detail: {
              code: "brief_figure_limit_exceeded",
              message:
                "Une brève ne peut contenir plus de trois figures intégrées.",
            },
          },
        });
        return;
      }
      proposal = createProposal(body);
      await route.fulfill({ status: 201, json: proposal });
      return;
    }
    if (method === "GET" && url.pathname === proposalPath) {
      await route.fulfill({ json: proposal });
      return;
    }
    if (method === "POST" && url.pathname === acceptPath) {
      if (!proposalInput)
        throw new Error("The proposal should exist before acceptance");
      proposal = { ...proposal, state: "accepted", state_version: 2 };
      snapshot = createSnapshot("article", 0, proposalInput.figures);
      await route.fulfill({
        json: {
          id: proposalId,
          state: "accepted",
          state_version: 2,
          artifact_id: snapshot.synthesis_artifact_id,
          artifact_version: snapshot.synthesis_version,
          snapshot_id: snapshot.snapshot_id,
          impact_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
          impact_state: "pending",
        },
      });
      return;
    }
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });
  return { unexpectedRequests, proposalRequests };
}

test("intègre deux figures après le paragraphe, les compare puis les accepte dans l’ordre du snapshot", async ({
  page,
}) => {
  const { unexpectedRequests, proposalRequests } = await installRoutes(page);
  const shotsDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-t-m6-25",
  );
  await mkdir(shotsDirectory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1400 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const anchor = page.locator(
    `.editorial-document__block[data-block-id="${anchorId}"]`,
  );
  await anchor.click();
  await expect(anchor).toHaveAttribute("data-selected", "true");
  await page.getByRole("button", { name: "Ajouter après" }).click();
  await page.getByRole("menuitem", { name: "Image issue des sources" }).click();

  const gallery = page.getByRole("dialog", {
    name: "Galerie d’images sources",
  });
  await expect(gallery).toBeVisible();
  await gallery
    .getByRole("checkbox", { name: "Sélectionner Carte des échanges" })
    .check();
  await gallery
    .getByRole("checkbox", { name: "Sélectionner Schéma de l’infrastructure" })
    .check();
  await expect(gallery.getByText("2 images sélectionnées")).toBeVisible();
  await page.screenshot({
    path: resolve(shotsDirectory, "galerie-deux-figures-desktop.png"),
  });
  await gallery
    .getByRole("button", { name: "Fermer la galerie d’images sources" })
    .click();

  await page
    .getByRole("textbox", {
      name: "Légende éditoriale pour Carte des échanges",
    })
    .fill("Carte des échanges observés");
  await page
    .getByRole("textbox", {
      name: "Légende éditoriale pour Schéma de l’infrastructure",
    })
    .fill("Schéma de l’infrastructure observée");
  await expect(
    page
      .getByText(
        `Emplacement · après « ${"Paragraphe courant où les images seront ajoutées."} »`,
      )
      .first(),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Intégrer les images sélectionnées" })
    .click();

  const comparison = page.getByRole("region", {
    name: "Comparaison de l’insertion de figures",
  });
  await expect(comparison).toContainText("Carte des échanges observés");
  await expect(comparison).toContainText("Schéma de l’infrastructure observée");
  await expect(comparison).toContainText(
    "Figure archivée et vérifiée dans la source synthétique.",
  );
  const comparisonHandle = await comparison.elementHandle();
  if (!comparisonHandle) throw new Error("Figure comparison is not rendered");
  await page
    .locator(".editorial-workspace__inspector-column")
    .evaluate((container, target) => {
      const inspector = container as unknown as ScrollableBounds;
      const comparisonTarget = target as unknown as ScrollBounds;
      inspector.scrollTop +=
        comparisonTarget.getBoundingClientRect().top -
        inspector.getBoundingClientRect().top;
    }, comparisonHandle);
  await page.screenshot({
    path: resolve(shotsDirectory, "proposition-figures-desktop.png"),
  });
  expect(proposalRequests).toHaveLength(1);
  expect(proposalRequests[0]).toEqual({
    snapshot_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    anchor_block_id: anchorId,
    figures: [
      {
        figure_id: figureIds[0],
        caption: "Carte des échanges observés",
        order: 0,
      },
      {
        figure_id: figureIds[1],
        caption: "Schéma de l’infrastructure observée",
        order: 1,
      },
    ],
  });

  await page.getByRole("button", { name: "Accepter" }).click();
  const firstFigure = page.locator(
    '.editorial-document__block[data-block-id="inserted-figure-1"]',
  );
  const secondFigure = page.locator(
    '.editorial-document__block[data-block-id="inserted-figure-2"]',
  );
  await expect(firstFigure).toContainText("Carte des échanges observés");
  await expect(secondFigure).toContainText(
    "Schéma de l’infrastructure observée",
  );
  const blockOrder = await page
    .locator(".editorial-document__block")
    .evaluateAll((blocks) =>
      blocks.map((block) =>
        (
          block as { getAttribute: (name: string) => string | null }
        ).getAttribute("data-block-id"),
      ),
    );
  expect(blockOrder).toEqual([
    paragraphIds[0],
    anchorId,
    "inserted-figure-1",
    "inserted-figure-2",
    paragraphIds[2],
  ]);
  await expect(
    firstFigure.getByRole("img", { name: "Carte des échanges observés" }),
  ).toHaveAttribute(
    "src",
    `/api/subjects/${subjectId}/editor/figures/${figureIds[0]}/thumbnail`,
  );
  await expect
    .poll(() =>
      firstFigure
        .getByRole("img", { name: "Carte des échanges observés" })
        .evaluate((image) => (image as { naturalWidth: number }).naturalWidth),
    )
    .toBeGreaterThan(0);
  await expect
    .poll(() =>
      secondFigure
        .getByRole("img", { name: "Schéma de l’infrastructure observée" })
        .evaluate((image) => (image as { naturalWidth: number }).naturalWidth),
    )
    .toBeGreaterThan(0);
  await firstFigure.scrollIntoViewIfNeeded();
  await expect(
    firstFigure.getByRole("img", { name: "Carte des échanges observés" }),
  ).toBeInViewport({ ratio: 1 });
  await expect(
    secondFigure.getByRole("img", {
      name: "Schéma de l’infrastructure observée",
    }),
  ).toBeInViewport({ ratio: 1 });
  await page.screenshot({
    path: resolve(shotsDirectory, "document-figures-integrees-desktop.png"),
  });
  expect(unexpectedRequests).toEqual([]);
});

test("explains a non-eligible figure and keeps its checkbox disabled", async ({
  page,
}) => {
  const { unexpectedRequests } = await installRoutes(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await page
    .locator(`.editorial-document__block[data-block-id="${anchorId}"]`)
    .click();
  await page.getByRole("button", { name: "Ajouter après" }).click();
  await page.getByRole("menuitem", { name: "Image issue des sources" }).click();

  const gallery = page.getByRole("dialog", {
    name: "Galerie d’images sources",
  });
  const unavailableFigure = gallery.getByRole("checkbox", {
    name: "Sélectionner Capture non admissible",
  });
  await expect(unavailableFigure).toBeDisabled();
  await expect(
    gallery.getByText(
      "La licence de cette figure ne permet pas son intégration.",
    ),
  ).toBeVisible();
  expect(unexpectedRequests).toEqual([]);
});

test("shows the server refusal when a stale BRIEF snapshot would add a fourth figure", async ({
  page,
}) => {
  const { unexpectedRequests, proposalRequests } = await installRoutes(page, {
    kind: "brief",
    existingFigures: 2,
    refuseBriefLimit: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await page
    .locator(`.editorial-document__block[data-block-id="${anchorId}"]`)
    .click();
  await page.getByRole("button", { name: "Ajouter après" }).click();
  await page.getByRole("menuitem", { name: "Image issue des sources" }).click();

  const gallery = page.getByRole("dialog", {
    name: "Galerie d’images sources",
  });
  await gallery
    .getByRole("checkbox", { name: "Sélectionner Carte des échanges" })
    .check();
  await gallery
    .getByRole("button", { name: "Fermer la galerie d’images sources" })
    .click();
  await page
    .getByRole("button", { name: "Intégrer les images sélectionnées" })
    .click();

  await expect(
    page
      .getByRole("alert")
      .getByText("Une brève ne peut contenir plus de trois figures intégrées."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Actualiser l’atelier" }).click();
  await page.mouse.move(0, 0);
  await page
    .locator(`.editorial-document__block[data-block-id="${anchorId}"]`)
    .click();
  await page.getByRole("button", { name: "Ajouter après" }).click();
  await page.getByRole("menuitem", { name: "Image issue des sources" }).click();
  await expect(
    page.getByText(/Capacité du document : 3\/3 figures intégrées/).first(),
  ).toBeVisible();
  expect(proposalRequests).toHaveLength(1);
  expect(unexpectedRequests).toEqual([]);
});
