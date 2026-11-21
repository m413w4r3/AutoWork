import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import type {
  EditorialManualTableProposalInput,
  EditorialProposalRead,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
  fulfillEditorialReadStatuses,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const titleId = "00000000-0000-4000-8000-000000000000";
const sectionId = "11111111-1111-4111-8111-111111111111";
const paragraphIds = [
  "22222222-2222-4222-8222-222222222221",
  "22222222-2222-4222-8222-222222222222",
  "22222222-2222-4222-8222-222222222223",
  "22222222-2222-4222-8222-222222222224",
];
const tableBlockId = "55555555-5555-4555-8555-555555555555";
const proposalId = "66666666-6666-4666-8666-666666666666";
const evidence = {
  source_document_id: "77777777-7777-4777-8777-777777777777",
  kind: "fact" as const,
  evidence_key: "a".repeat(64),
};
const snapshotPath = `/api/subjects/${subjectId}/editor/snapshot`;
const threadPath = `/api/subjects/${subjectId}/editor/threads/${paragraphIds[2]}`;
const proposalPath = `${threadPath}/proposals/${proposalId}`;
const tableProposalPath = `/api/subjects/${subjectId}/editor/blocks/${paragraphIds[2]}/table-proposals`;
const acceptPath = `${proposalPath}/accept`;

function createSnapshot(withTable: boolean): EditorialWorkspaceSnapshot {
  const paragraphs = paragraphIds.map((blockId, index) => ({
    block_id: blockId,
    kind: "paragraph" as const,
    parent_block_id: sectionId,
    position: index + 2,
    text: `Paragraphe synthétique ${index + 1} avec preuve admise.`,
    chart: null,
    evidence_refs:
      index === 2
        ? [
            {
              ...evidence,
              source_title: "Source synthétique",
              passage: "Un fait vérifiable est rapporté.",
            },
          ]
        : [],
    table: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: "b".repeat(64),
    allowed_operations: index === 2 ? ["insert_after", "create_table"] : [],
    revision_status: "ready" as const,
  }));
  const tableBlock = {
    block_id: tableBlockId,
    kind: "table" as const,
    parent_block_id: sectionId,
    position: 5,
    text: "Résumé synthétique",
    evidence_refs: [evidence],
    chart: null,
    table: {
      key: "table-synthetic",
      kind: "custom" as const,
      title: "Résumé synthétique",
      caption: "Informations rapportées",
      columns: [
        { key: "element", label: "Élément" },
        { key: "value", label: "Valeur" },
      ],
      rows: [
        {
          cells: ["Échantillon", "Confirmé"],
          evidence_refs: [evidence],
        },
      ],
      evidence_refs: [evidence],
      anchor_block_id: paragraphIds[2] ?? null,
      placement: "after_paragraph" as const,
    },
    revision_target: { block_id: tableBlockId, legacy_target_id: null },
    before_hash: null,
    allowed_operations: [],
    revision_status: "ready" as const,
  };
  const orderedBlocks = withTable
    ? [
        {
          block_id: titleId,
          kind: "title" as const,
          parent_block_id: null,
          position: 0,
          text: "Article synthétique",
          evidence_refs: [],
          table: null,
          chart: null,
          revision_target: { block_id: titleId, legacy_target_id: null },
          before_hash: null,
          allowed_operations: [],
          revision_status: "ready" as const,
        },
        {
          block_id: sectionId,
          kind: "section_heading" as const,
          parent_block_id: null,
          position: 1,
          text: "Contexte",
          evidence_refs: [],
          table: null,
          chart: null,
          revision_target: { block_id: sectionId, legacy_target_id: null },
          before_hash: null,
          allowed_operations: [],
          revision_status: "ready" as const,
        },
        paragraphs[0],
        paragraphs[1],
        paragraphs[2],
        tableBlock,
        paragraphs[3],
      ]
    : [
        {
          block_id: titleId,
          kind: "title" as const,
          parent_block_id: null,
          position: 0,
          text: "Article synthétique",
          evidence_refs: [],
          table: null,
          chart: null,
          revision_target: { block_id: titleId, legacy_target_id: null },
          before_hash: null,
          allowed_operations: [],
          revision_status: "ready" as const,
        },
        {
          block_id: sectionId,
          kind: "section_heading" as const,
          parent_block_id: null,
          position: 1,
          text: "Contexte",
          evidence_refs: [],
          table: null,
          chart: null,
          revision_target: { block_id: sectionId, legacy_target_id: null },
          before_hash: null,
          allowed_operations: [],
          revision_status: "ready" as const,
        },
        ...paragraphs,
      ];
  return {
    subject_id: subjectId,
    production_run_id: "88888888-8888-4888-8888-888888888888",
    publication_kind: "article",
    synthesis_artifact_id: "99999999-9999-4999-8999-999999999999",
    synthesis_version: withTable ? 2 : 1,
    synthesis_canonical_sha256: withTable ? "c".repeat(64) : "b".repeat(64),
    enrichment_artifact_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    enrichment_version: withTable ? 2 : 1,
    publication_artifact_id: null,
    publication_version: null,
    pipeline_generation: 1,
    blocks: orderedBlocks,
    enrichments: [],
    document_outline: [
      {
        block_id: sectionId,
        label: "Contexte",
        children: paragraphs.map((paragraph) => paragraph.block_id),
      },
    ],
    capabilities: {
      can_discuss: true,
      can_preview_revision: false,
      manual_edit: true,
      can_add_enrichment: true,
      can_preview_pdf: true,
      can_regenerate_pdf: false,
    },
    preview_status: "READY",
    impact_status: withTable ? "pending" : "none",
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
    snapshot_id: withTable
      ? "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
      : "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
  };
}

function createTableProposal(): EditorialProposalRead {
  const anchorBlockId = paragraphIds[2];
  if (!anchorBlockId) throw new Error("Missing the third paragraph fixture");
  return {
    id: proposalId,
    subject_id: subjectId,
    production_run_id: "88888888-8888-4888-8888-888888888888",
    occurrence_id: anchorBlockId,
    thread_id: null,
    origin: "manual_patch",
    state: "proposed",
    state_version: 1,
    operation: "create_table",
    base_artifact_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    base_artifact_version: 1,
    base_canonical_sha256: "d".repeat(64),
    base_text: null,
    patch_sha256: "e".repeat(64),
    patch: {
      schema_version: "editorial-patch-v1",
      operation: "create_table",
      target: {
        kind: "enrichment_element",
        placement: { kind: "after_paragraph", anchor_block_id: anchorBlockId },
      },
      target_id: "target-table-synthetic",
      occurrence_id: anchorBlockId,
      before_hash: "f".repeat(64),
      rationale: "Tableau manuel appuyé sur les preuves admises.",
      enrichment_patch: {
        tables: [
          {
            key: "table-synthetic",
            kind: "custom",
            title: "Résumé synthétique",
            caption: "Informations rapportées",
            columns: [
              { key: "element", label: "Élément" },
              { key: "value", label: "Valeur" },
            ],
            rows: [
              {
                cells: ["Échantillon", "Confirmé"],
                evidence_handles: ["fact-1"],
              },
            ],
            placement: {
              kind: "after_paragraph",
              anchor_block_id: anchorBlockId,
            },
            purpose: {
              question: "Quelles informations sont rapportées ?",
              available_data: "Informations du paragraphe.",
              comprehension_gain: "Lecture structurée.",
              scope: "Paragraphe sélectionné.",
              evidence_handles: ["fact-1"],
              knowledge_limits: "Aucune extrapolation.",
              placement_reason: "Après le paragraphe sélectionné.",
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

test("a table proposal is accepted after paragraph three in snapshot order", async ({
  page,
}) => {
  let snapshot = createSnapshot(false);
  let proposal = createTableProposal();
  const requests: string[] = [];
  const unexpectedRequests: string[] = [];
  const tableRequests: EditorialManualTableProposalInput[] = [];

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
    if (
      await fulfillEditorialReadStatuses(route, method, url.pathname, snapshot)
    ) {
      return;
    }
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
        occurrence_id: paragraphIds[2],
        artifact_id: snapshot.synthesis_artifact_id,
        artifact_version: snapshot.synthesis_version,
        canonical_sha256: snapshot.synthesis_canonical_sha256,
      };
      await route.fulfill({
        headers: { ETag: '"table-placement-thread"' },
        json: {
          kind: "unopened",
          binding,
          current: binding,
          bridge_status: "ready",
          event_sequence: 0,
          timeline: [],
          etag: '"table-placement-thread"',
        },
      });
      return;
    }
    if (method === "POST" && url.pathname === tableProposalPath) {
      tableRequests.push(
        route.request().postDataJSON() as EditorialManualTableProposalInput,
      );
      await route.fulfill({ status: 201, json: proposal });
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

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  const thirdParagraphId = paragraphIds[2];
  if (!thirdParagraphId) throw new Error("Missing the third paragraph fixture");
  const thirdParagraph = page.locator(
    `.editorial-document__block[data-block-id="${thirdParagraphId}"]`,
  );
  await thirdParagraph.click();
  await expect(thirdParagraph).toHaveAttribute("data-selected", "true");
  await page.getByRole("button", { name: "Ajouter après" }).click();
  await page.getByRole("menuitem", { name: "Tableau" }).click();

  const shotsDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-t-m5-22",
  );
  await mkdir(shotsDirectory, { recursive: true });
  await page.screenshot({
    path: resolve(shotsDirectory, "composeur-desktop.png"),
    fullPage: true,
  });

  await page
    .getByRole("textbox", { name: "Titre du tableau" })
    .fill("Résumé synthétique");
  await page
    .getByRole("textbox", { name: "Légende" })
    .fill("Informations rapportées");
  await page
    .getByRole("textbox", { name: "Intitulé de colonne 1" })
    .fill("Élément");
  await page
    .getByRole("textbox", { name: "Intitulé de colonne 2" })
    .fill("Valeur");
  await page
    .getByRole("textbox", { name: "Cellule ligne 1, colonne 1" })
    .fill("Échantillon");
  await page
    .getByRole("textbox", { name: "Cellule ligne 1, colonne 2" })
    .fill("Confirmé");
  await page.getByRole("checkbox", { name: /Source synthétique/ }).check();
  await page.getByRole("button", { name: "Proposer" }).click();
  await expect(
    page
      .getByRole("region", { name: "Comparaison de l’insertion de tableau" })
      .getByRole("table", { name: "Résumé synthétique" }),
  ).toBeVisible();
  await expect(page.getByText("Après ce paragraphe")).toBeVisible();
  await page.getByText("Preuves associées", { exact: true }).click();
  await page.locator(".editorial-proposal-card").screenshot({
    path: resolve(shotsDirectory, "proposition-apercu-desktop.png"),
  });

  const tableRequest = tableRequests[0];
  if (!tableRequest) throw new Error("Expected the table proposal request");
  expect(tableRequest.table.rows[0]?.evidence_refs).toEqual([evidence]);
  await page.getByRole("button", { name: "Accepter" }).click();

  const insertedTable = page.locator(
    `.editorial-document__block[data-block-id="${tableBlockId}"]`,
  );
  await expect(insertedTable).toContainText("Résumé synthétique");
  await expect(thirdParagraph).toHaveAttribute("data-selected", "true");
  const documentBlocks = await page.locator(".editorial-document__block").all();
  const blockOrder = await Promise.all(
    documentBlocks.map((block) => block.getAttribute("data-block-id")),
  );
  expect(blockOrder).toEqual([
    titleId,
    sectionId,
    paragraphIds[0],
    paragraphIds[1],
    thirdParagraphId,
    tableBlockId,
    paragraphIds[3],
  ]);
  await page.getByRole("button", { name: "Fermer" }).click();
  await page.evaluate("window.scrollTo(0, 0)");
  await page.screenshot({
    path: resolve(shotsDirectory, "document-tableau-insere-desktop.png"),
    fullPage: false,
  });
  expect(requests.some((request) => request.includes("/entries"))).toBe(false);
  expect(unexpectedRequests).toEqual([]);
});
