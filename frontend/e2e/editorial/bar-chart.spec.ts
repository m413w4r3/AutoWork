import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import type {
  EditorialManualBarChartProposalInput,
  EditorialProposalRead,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const titleId = "00000000-0000-4000-8000-000000000000";
const sectionId = "11111111-1111-4111-8111-111111111111";
const paragraphIds = [
  "22222222-2222-4222-8222-222222222221",
  "22222222-2222-4222-8222-222222222222",
  "22222222-2222-4222-8222-222222222223",
];
const chartBlockId = "55555555-5555-4555-8555-555555555555";
const proposalId = "66666666-6666-4666-8666-666666666666";
const anchorId = paragraphIds[2];
if (!anchorId) throw new Error("Missing the third paragraph fixture");

const evidence = {
  source_document_id: "77777777-7777-4777-8777-777777777777",
  kind: "fact" as const,
  evidence_key: "a".repeat(64),
};
const snapshotPath = `/api/subjects/${subjectId}/editor/snapshot`;
const threadPath = `/api/subjects/${subjectId}/editor/threads/${anchorId}`;
const proposalPath = `${threadPath}/proposals/${proposalId}`;
const chartProposalPath = `/api/subjects/${subjectId}/editor/blocks/${anchorId}/bar-chart-proposals`;
const acceptPath = `${proposalPath}/accept`;
const insufficientDataMessage =
  "Aucun graphique quantitatif vérifiable ne peut être créé à partir des preuves actuelles. Un tableau descriptif peut être proposé à la place.";

function createSnapshot(withChart: boolean): EditorialWorkspaceSnapshot {
  const paragraphs = paragraphIds.map((blockId, index) => ({
    block_id: blockId,
    kind: "paragraph" as const,
    parent_block_id: sectionId,
    position: index + 2,
    text: `Paragraphe synthétique ${index + 1} avec preuve admise.`,
    evidence_refs:
      index === 2
        ? [
            {
              ...evidence,
              source_title: "Source synthétique",
              passage: "Valeurs mesurées et rapportées par la source.",
            },
          ]
        : [],
    table: null,
    chart: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: "b".repeat(64),
    allowed_operations:
      index === 2 ? ["insert_after", "create_table", "create_bar_chart"] : [],
    revision_status: "ready" as const,
  }));
  const chartBlock = {
    block_id: chartBlockId,
    kind: "chart" as const,
    parent_block_id: sectionId,
    position: 5,
    text: "Valeurs documentées",
    evidence_refs: [evidence],
    table: null,
    chart: {
      key: "chart-synthetic",
      kind: "bar" as const,
      title: "Valeurs documentées",
      caption: null,
      legend: ["Mesures vérifiées"],
      category_axis_label: "Indicateur",
      value_axis_label: "Valeur mesurée",
      unit: "unités",
      points: [
        { label: "Premier", value: "4", evidence_refs: [evidence] },
        { label: "Second", value: "8", evidence_refs: [evidence] },
      ],
      anchor_block_id: anchorId,
    },
    revision_target: { block_id: chartBlockId, legacy_target_id: null },
    before_hash: null,
    allowed_operations: [],
    revision_status: "ready" as const,
  };
  return {
    subject_id: subjectId,
    production_run_id: "88888888-8888-4888-8888-888888888888",
    publication_kind: "article",
    synthesis_artifact_id: "99999999-9999-4999-8999-999999999999",
    synthesis_version: withChart ? 2 : 1,
    synthesis_canonical_sha256: withChart ? "c".repeat(64) : "b".repeat(64),
    enrichment_artifact_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    enrichment_version: withChart ? 2 : 1,
    publication_artifact_id: null,
    publication_version: null,
    pipeline_generation: 1,
    blocks: [
      {
        block_id: titleId,
        kind: "title",
        parent_block_id: null,
        position: 0,
        text: "Article synthétique",
        evidence_refs: [],
        table: null,
        chart: null,
        revision_target: { block_id: titleId, legacy_target_id: null },
        before_hash: null,
        allowed_operations: [],
        revision_status: "ready",
      },
      {
        block_id: sectionId,
        kind: "section_heading",
        parent_block_id: null,
        position: 1,
        text: "Contexte",
        evidence_refs: [],
        table: null,
        chart: null,
        revision_target: { block_id: sectionId, legacy_target_id: null },
        before_hash: null,
        allowed_operations: [],
        revision_status: "ready",
      },
      ...paragraphs.slice(0, 2),
      paragraphs[2],
      ...(withChart ? [chartBlock] : []),
    ],
    enrichments: [],
    document_outline: [
      {
        block_id: sectionId,
        label: "Contexte",
        children: paragraphIds,
      },
    ],
    capabilities: {
      can_discuss: true,
      can_preview_revision: false,
      manual_edit: true,
      can_add_enrichment: true,
      can_preview_pdf: true,
    },
    preview_status: "READY",
    impact_status: withChart ? "pending" : "none",
    pdf_status: "CURRENT",
    snapshot_id: withChart
      ? "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
      : "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
  };
}

function createChartProposal(): EditorialProposalRead {
  return {
    id: proposalId,
    subject_id: subjectId,
    production_run_id: "88888888-8888-4888-8888-888888888888",
    occurrence_id: anchorId,
    thread_id: null,
    origin: "manual_patch",
    state: "proposed",
    state_version: 1,
    operation: "create_bar_chart",
    base_artifact_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    base_artifact_version: 1,
    base_canonical_sha256: "d".repeat(64),
    base_text: null,
    patch_sha256: "e".repeat(64),
    patch: {
      schema_version: "editorial-patch-v1",
      operation: "create_bar_chart",
      target: {
        kind: "enrichment_element",
        placement: { kind: "after_paragraph", anchor_block_id: anchorId },
      },
      target_id: "target-chart-synthetic",
      occurrence_id: anchorId,
      before_hash: "f".repeat(64),
      rationale: "Graphique manuel étayé par les preuves admises.",
      enrichment_patch: {
        charts: [
          {
            key: "chart-synthetic",
            kind: "bar",
            title: "Valeurs documentées",
            caption: null,
            category_axis_label: "Indicateur",
            value_axis_label: "Valeur mesurée",
            unit: "unités",
            points: [
              {
                label: "Premier",
                series: "Mesures vérifiées",
                value: "4",
                evidence_handles: ["fact-1"],
              },
              {
                label: "Second",
                series: "Mesures vérifiées",
                value: "8",
                evidence_handles: ["fact-1"],
              },
            ],
            placement: { kind: "after_paragraph", anchor_block_id: anchorId },
            purpose: {
              question: "Comparer les valeurs rapportées.",
              evidence_handles: ["fact-1"],
            },
          },
        ],
      },
      evidence_refs: [evidence],
    },
    diagnostics: {},
    created_at: "2026-11-21T12:00:00Z",
  };
}

async function installRoutes(page: Page, respondWithInsufficientData = false) {
  let snapshot = createSnapshot(false);
  let proposal = createChartProposal();
  const requests: string[] = [];
  const chartRequests: EditorialManualBarChartProposalInput[] = [];
  const unexpectedRequests: string[] = [];

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
    requests.push(`${method} ${url.pathname}`);
    if (method === "GET" && url.pathname === snapshotPath) {
      await route.fulfill({ json: snapshot });
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
        headers: { ETag: '"bar-chart-thread"' },
        json: {
          kind: "unopened",
          binding,
          current: binding,
          bridge_status: "ready",
          event_sequence: 0,
          timeline: [],
          etag: '"bar-chart-thread"',
        },
      });
      return;
    }
    if (method === "POST" && url.pathname === chartProposalPath) {
      chartRequests.push(
        route.request().postDataJSON() as EditorialManualBarChartProposalInput,
      );
      if (respondWithInsufficientData) {
        await route.fulfill({
          status: 422,
          json: {
            detail: {
              code: "bar_chart_insufficient_data",
              message: insufficientDataMessage,
            },
          },
        });
      } else {
        await route.fulfill({ status: 201, json: proposal });
      }
      return;
    }
    if (method === "GET" && url.pathname === proposalPath) {
      await route.fulfill({ json: proposal });
      return;
    }
    if (method === "POST" && url.pathname === acceptPath) {
      proposal = { ...proposal, state: "accepted", state_version: 2 };
      snapshot = createSnapshot(true);
      await route.fulfill({
        json: {
          id: proposalId,
          state: "accepted",
          state_version: 2,
          artifact_id: snapshot.synthesis_artifact_id,
          artifact_version: snapshot.synthesis_version,
          snapshot_id: snapshot.snapshot_id,
          impact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
          impact_state: "pending",
        },
      });
      return;
    }
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });

  return {
    chartRequests,
    requests,
    unexpectedRequests,
  };
}

async function openChartComposer(page: Page) {
  const anchor = page.locator(
    `.editorial-document__block[data-block-id="${anchorId}"]`,
  );
  await anchor.click();
  await expect(anchor).toHaveAttribute("data-selected", "true");
  await page.getByRole("button", { name: "Ajouter après" }).click();
  await page.getByRole("menuitem", { name: "Graphique" }).click();
  return anchor;
}

async function fillChartForm(page: Page) {
  await page
    .getByRole("textbox", { name: "Titre du graphique" })
    .fill("Valeurs documentées");
  await page
    .getByRole("textbox", { name: "Légende" })
    .fill("Mesures vérifiées");
  await page
    .getByRole("textbox", { name: "Intitulé des catégories" })
    .fill("Indicateur");
  await page
    .getByRole("textbox", { name: "Intitulé des valeurs" })
    .fill("Valeur mesurée");
  await page.getByRole("textbox", { name: "Unité" }).fill("unités");
  await page
    .getByRole("textbox", { name: "Libellé du point 1" })
    .fill("Premier");
  await page.getByRole("spinbutton", { name: "Valeur du point 1" }).fill("4");
  await page
    .getByRole("checkbox", { name: /Source synthétique/ })
    .nth(0)
    .check();
  await page
    .getByRole("textbox", { name: "Libellé du point 2" })
    .fill("Second");
  await page.getByRole("spinbutton", { name: "Valeur du point 2" }).fill("8");
  await page
    .getByRole("checkbox", { name: /Source synthétique/ })
    .nth(1)
    .check();
}

test("accepts a supported bar chart and renders it after its anchor in snapshot order", async ({
  page,
}) => {
  const { chartRequests, requests, unexpectedRequests } =
    await installRoutes(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const anchor = await openChartComposer(page);
  await fillChartForm(page);
  const shotsDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-t-m5-23",
  );
  await mkdir(shotsDirectory, { recursive: true });
  await page.locator(".editorial-bar-chart").first().scrollIntoViewIfNeeded();
  await page.screenshot({
    path: resolve(shotsDirectory, "composeur-graphique-desktop.png"),
    fullPage: false,
  });

  await page.getByRole("button", { name: "Proposer" }).click();
  const comparison = page.getByRole("region", {
    name: "Comparaison de l’insertion de graphique",
  });
  await expect(
    comparison.getByRole("figure", { name: "Graphique : Valeurs documentées" }),
  ).toBeVisible();
  await expect(comparison.getByText("Mesures vérifiées")).toBeVisible();
  await expect(comparison.getByText("8 unités")).toBeVisible();
  await page.locator(".editorial-proposal-card").scrollIntoViewIfNeeded();
  await page.screenshot({
    path: resolve(shotsDirectory, "proposition-graphique-desktop.png"),
    fullPage: false,
  });
  await comparison.getByText("Preuves associées", { exact: true }).click();
  await expect(comparison.getByText(evidence.evidence_key)).toBeVisible();
  await comparison.getByText("Preuves associées", { exact: true }).click();

  const submitted = chartRequests[0];
  if (!submitted) throw new Error("Expected the manual chart proposal request");
  expect(submitted.snapshot_id).toBe("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab");
  expect(submitted.anchor_block_id).toBe(anchorId);
  expect(submitted.chart.points.map((point) => point.evidence_refs)).toEqual([
    [evidence],
    [evidence],
  ]);
  expect(submitted.chart.points.map((point) => point.value)).toEqual([
    "4",
    "8",
  ]);
  await page.getByRole("button", { name: "Accepter" }).click();

  const chartBlock = page.locator(
    `.editorial-document__block[data-block-id="${chartBlockId}"]`,
  );
  await expect(
    chartBlock.getByRole("figure", { name: "Graphique : Valeurs documentées" }),
  ).toBeVisible();
  await expect(anchor).toHaveAttribute("data-selected", "true");
  const documentBlocks = await page.locator(".editorial-document__block").all();
  const blockOrder = await Promise.all(
    documentBlocks.map((block) => block.getAttribute("data-block-id")),
  );
  expect(blockOrder).toEqual([
    titleId,
    sectionId,
    paragraphIds[0],
    paragraphIds[1],
    anchorId,
    chartBlockId,
  ]);
  await chartBlock.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: resolve(shotsDirectory, "document-graphique-insere-desktop.png"),
    fullPage: false,
  });
  expect(requests.some((request) => request.includes("/entries"))).toBe(false);
  expect(unexpectedRequests).toEqual([]);
});

test("offers a descriptive table when the chart endpoint refuses insufficient data", async ({
  page,
}) => {
  const { chartRequests, unexpectedRequests } = await installRoutes(page, true);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await openChartComposer(page);
  await fillChartForm(page);
  await page.getByRole("button", { name: "Proposer" }).click();
  await expect(page.getByRole("alert")).toHaveText(insufficientDataMessage);
  await page
    .getByRole("button", { name: "Proposer un tableau à la place" })
    .click();
  await expect(
    page.getByRole("region", { name: "Créer un tableau" }),
  ).toBeVisible();
  expect(chartRequests).toHaveLength(1);
  expect(unexpectedRequests).toEqual([]);
});
