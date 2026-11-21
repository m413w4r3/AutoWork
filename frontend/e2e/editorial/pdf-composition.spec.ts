import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import type {
  EditorialCompositionStatusView,
  EditorialHistoryItem,
  EditorialPdfStatusView,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const editionId = EDITORIAL_FIXTURES.subject.edition_id;
const blockId = "22222222-2222-4222-8222-222222222222";
const publicationId = "33333333-3333-4333-8333-333333333333";
const pdfId = "44444444-4444-4444-8444-444444444444";

function makeSnapshot(
  version: number,
  text: string,
  status: EditorialWorkspaceSnapshot["preview_status"],
): EditorialWorkspaceSnapshot {
  const isReady = status === "READY";
  return {
    subject_id: subjectId,
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    publication_kind: "article",
    synthesis_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    synthesis_version: version,
    synthesis_canonical_sha256: version === 1 ? "a".repeat(64) : "b".repeat(64),
    enrichment_artifact_id: null,
    enrichment_version: null,
    publication_artifact_id: publicationId,
    publication_version: version,
    pipeline_generation: 1,
    blocks: [
      {
        block_id: blockId,
        kind: "paragraph",
        parent_block_id: null,
        position: 0,
        text,
        evidence_refs: [],
        chart: null,
        revision_target: { block_id: blockId, legacy_target_id: null },
        before_hash: "d".repeat(64),
        allowed_operations: ["rewrite_paragraph"],
        revision_status: "ready",
      },
    ],
    enrichments: [],
    document_outline: [],
    capabilities: {
      can_discuss: false,
      can_preview_revision: false,
      manual_edit: true,
      can_add_enrichment: false,
      can_preview_pdf: true,
      can_regenerate_pdf: true,
    },
    preview_status: status,
    impact_status: "none",
    pdf_status: isReady ? "CURRENT" : "STALE",
    pdf_artifact_id: isReady ? pdfId : null,
    pdf_artifact_version: isReady ? version : null,
    pdf_artifact_input_hash: isReady ? "e".repeat(64) : null,
    pdf_artifact_canonical_sha256: isReady ? "f".repeat(64) : null,
    pdf_render_id: isReady ? "55555555-5555-4555-8555-555555555555" : null,
    pdf_render_identity: isReady ? `render-v${version}` : null,
    pdf_error_code: null,
    pdf_error_message: null,
    pdf_url: isReady
      ? `/api/subjects/${subjectId}/editor/pdf/download?version=${version}`
      : null,
    snapshot_id:
      version === 1
        ? "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        : "ffffffff-ffff-4fff-8fff-ffffffffffff",
  };
}

function historyItem(
  overrides: Partial<EditorialHistoryItem>,
): EditorialHistoryItem {
  return {
    proposal_id: "66666666-6666-4666-8666-666666666666",
    subject_id: subjectId,
    occurrence_id: blockId,
    state: "accepted",
    origin: "manual_patch",
    operation: "rewrite_paragraph",
    operation_label: "Réécriture",
    actor_id: "77777777-7777-4777-8777-777777777777",
    actor_display: "Opérateur · 77777777",
    created_at: "2026-11-20T09:30:00Z",
    updated_at: "2026-11-20T09:30:01Z",
    canonical_result_artifact_id: publicationId,
    canonical_result_version: 2,
    canonical_result_stage: "synthesis",
    thread_id: null,
    thread_url: null,
    ...overrides,
  };
}

const compositionStatus: EditorialCompositionStatusView = {
  edition_id: editionId,
  subject_id: subjectId,
  status: "stale",
  message: "La composition utilise une ancienne version canonique.",
  composition_id: "88888888-8888-4888-8888-888888888888",
  composition_version: 4,
  pinned_artifact_id: publicationId,
  pinned_artifact_version: 1,
  current_artifact_id: publicationId,
  current_artifact_version: 2,
  current_version_available: true,
  composition_url: `/api/editions/${editionId}/composition`,
};

test("canonical adoption refreshes the PDF and keeps history and composition pins explicit", async ({
  page,
}) => {
  let snapshot = makeSnapshot(
    1,
    "Synthetic original paragraph for the PDF preview.",
    "READY",
  );
  let pdfStatus: EditorialPdfStatusView = {
    subject_id: subjectId,
    status: "READY",
    artifact_id: pdfId,
    artifact_version: 1,
    artifact_input_hash: "e".repeat(64),
    artifact_canonical_sha256: "f".repeat(64),
    render_id: "55555555-5555-4555-8555-555555555555",
    render_identity: "render-v1",
    error_code: null,
    error_message: null,
    pdf_url: `/api/subjects/${subjectId}/editor/pdf/download?version=1`,
  };
  const history = [
    historyItem({
      proposal_id: "99999999-9999-4999-8999-999999999999",
      state: "proposed",
      origin: "bridge_draft",
      operation: "create_table",
      operation_label: "Tableau",
      actor_display: "Opérateur · aaaaaaaa",
      canonical_result_artifact_id: null,
      canonical_result_version: null,
      canonical_result_stage: null,
      thread_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
      thread_url: `/api/subjects/${subjectId}/editor/threads/${blockId}`,
      created_at: "2026-11-20T10:30:00Z",
      updated_at: "2026-11-20T10:30:00Z",
    }),
    historyItem({}),
  ];
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

    if (
      method === "GET" &&
      url.pathname === `/api/subjects/${subjectId}/editor/snapshot`
    ) {
      await route.fulfill({ json: snapshot });
      return;
    }
    if (
      method === "GET" &&
      url.pathname === `/api/subjects/${subjectId}/editor/pdf`
    ) {
      await route.fulfill({ json: pdfStatus });
      return;
    }
    if (
      method === "POST" &&
      url.pathname === `/api/subjects/${subjectId}/editor/pdf/regenerate`
    ) {
      pdfStatus = {
        subject_id: subjectId,
        status: "GENERATING",
        artifact_id: null,
        artifact_version: null,
        artifact_input_hash: null,
        artifact_canonical_sha256: null,
        render_id: null,
        render_identity: null,
        error_code: null,
        error_message: null,
        pdf_url: null,
      };
      snapshot = makeSnapshot(
        2,
        "Synthetic manually adopted paragraph for the current PDF.",
        "READY",
      );
      pdfStatus = {
        subject_id: subjectId,
        status: "READY",
        artifact_id: pdfId,
        artifact_version: 2,
        artifact_input_hash: "1".repeat(64),
        artifact_canonical_sha256: "2".repeat(64),
        render_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbc",
        render_identity: "render-v2",
        error_code: null,
        error_message: null,
        pdf_url: `/api/subjects/${subjectId}/editor/pdf/download?version=2`,
      };
      await route.fulfill({ json: pdfStatus });
      return;
    }
    if (
      method === "GET" &&
      url.pathname === `/api/subjects/${subjectId}/editor/composition`
    ) {
      await route.fulfill({ json: compositionStatus });
      return;
    }
    if (
      method === "GET" &&
      url.pathname === `/api/subjects/${subjectId}/editor/history`
    ) {
      await route.fulfill({
        json: { items: history, next_cursor: null, limit: 20 },
      });
      return;
    }
    if (
      method === "GET" &&
      url.pathname ===
        `/api/subjects/${subjectId}/editor/blocks/${blockId}/draft`
    ) {
      await route.fulfill({
        status: 404,
        json: { detail: { code: "draft_not_found" } },
      });
      return;
    }
    if (
      method === "GET" &&
      url.pathname === `/api/subjects/${subjectId}/editor/threads/${blockId}`
    ) {
      const binding = {
        subject_id: subjectId,
        production_run_id: snapshot.production_run_id,
        occurrence_id: blockId,
        artifact_id: snapshot.synthesis_artifact_id,
        artifact_version: snapshot.synthesis_version,
        canonical_sha256: snapshot.synthesis_canonical_sha256,
      };
      await route.fulfill({
        headers: { ETag: '"pdf-composition-thread"' },
        json: {
          kind: "unopened",
          binding,
          current: binding,
          bridge_status: "ready",
          event_sequence: 0,
          timeline: [],
          etag: '"pdf-composition-thread"',
        },
      });
      return;
    }
    if (
      method === "POST" &&
      url.pathname ===
        `/api/subjects/${subjectId}/editor/blocks/${blockId}/manual-edit`
    ) {
      snapshot = makeSnapshot(
        2,
        "Synthetic manually adopted paragraph for the current PDF.",
        "STALE",
      );
      pdfStatus = {
        subject_id: subjectId,
        status: "STALE",
        artifact_id: pdfId,
        artifact_version: 1,
        artifact_input_hash: "e".repeat(64),
        artifact_canonical_sha256: "f".repeat(64),
        render_id: "55555555-5555-4555-8555-555555555555",
        render_identity: "render-v1",
        error_code: null,
        error_message: null,
        pdf_url: null,
      };
      await route.fulfill({
        json: {
          id: "cccccccc-cccc-4ccc-8ccc-cccccccccccd",
          state: "accepted",
          state_version: 2,
          artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
          artifact_version: 2,
          snapshot_id: snapshot.snapshot_id,
          impact_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
          impact_state: "none",
        },
      });
      return;
    }

    unexpectedRequests.push(`${method} ${url.pathname}${url.search}`);
    await route.abort("blockedbyclient");
  });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  const block = page.locator(
    `.editorial-document__block[data-block-id="${blockId}"]`,
  );
  await expect(block).toContainText("Synthetic original paragraph");
  await block.click();
  await page.getByRole("button", { name: "Modifier" }).click();
  await page
    .getByRole("textbox", { name: "Texte du bloc" })
    .fill("Synthetic manually adopted paragraph for the current PDF.");
  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();
  await expect(block).toContainText("Synthetic manually adopted paragraph");
  await expect(page.locator(".editorial-status")).toHaveText("PDF obsolète");
  await expect(
    page.getByRole("button", { name: "Régénérer le PDF" }),
  ).toBeEnabled();

  const shotsDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-t-m7-26",
  );
  await mkdir(shotsDirectory, { recursive: true });
  await page.screenshot({
    path: resolve(shotsDirectory, "pdf-obsolete-desktop.png"),
    fullPage: false,
  });

  await expect(page.getByText("Composition à mettre à jour")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Ouvrir la composition" }),
  ).toHaveAttribute("href", `/editions/${editionId}/composition`);
  await expect(
    page.getByRole("button", { name: /repin|repointer/i }),
  ).toHaveCount(0);

  await page.getByRole("button", { name: "Régénérer le PDF" }).click();
  await expect(page.locator(".editorial-status")).toHaveText("À jour");
  const pdfLink = page.getByRole("link", { name: "Voir le PDF" });
  await expect(pdfLink).toHaveAttribute(
    "href",
    `/api/subjects/${subjectId}/editor/pdf/download?version=2`,
  );
  await expect(page.getByText("Composition à mettre à jour")).toBeVisible();
  await page.screenshot({
    path: resolve(shotsDirectory, "composition-stale-desktop.png"),
    fullPage: false,
  });

  await page.reload();
  await page.getByRole("button", { name: "Historique et détails" }).click();
  await page.getByRole("menuitem", { name: "Historique" }).click();
  await expect(page.getByText("Tableau")).toBeVisible();
  await expect(page.getByText("Bridge", { exact: true })).toBeVisible();
  await expect(page.getByText("Réécriture")).toBeVisible();
  await expect(page.getByText("Modification manuelle")).toBeVisible();
  await expect(
    page
      .getByRole("region", { name: "Historique des propositions" })
      .getByText("Version canonique v2"),
  ).toBeVisible();
  await page.screenshot({
    path: resolve(shotsDirectory, "editorial-history-desktop.png"),
    fullPage: false,
  });
  expect(unexpectedRequests).toEqual([]);
});
