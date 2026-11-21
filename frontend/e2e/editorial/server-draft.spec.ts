import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import type { EditorialWorkspaceSnapshot } from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
  fulfillEditorialReadStatuses,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const titleId = "00000000-0000-4000-8000-000000000000";
const paragraphId = "22222222-2222-4222-8222-222222222222";

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: subjectId,
  production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  publication_kind: "article",
  synthesis_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  synthesis_version: 4,
  synthesis_canonical_sha256: "a".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 2,
  blocks: [
    {
      block_id: titleId,
      kind: "title",
      parent_block_id: null,
      position: 0,
      text: EDITORIAL_FIXTURES.article.title,
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: titleId, legacy_target_id: null },
      before_hash: null,
      allowed_operations: [],
      revision_status: "ready",
    },
    {
      block_id: paragraphId,
      kind: "paragraph",
      parent_block_id: null,
      position: 1,
      text: EDITORIAL_FIXTURES.article.paragraphs[0].text,
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: paragraphId, legacy_target_id: null },
      before_hash: null,
      allowed_operations: [],
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
  snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
};

test("server draft status is desktop-only and does not show draft prose", async ({
  page,
}) => {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== EDITORIAL_APP_ORIGIN) {
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
    if (
      method === "GET" &&
      url.pathname === `/api/subjects/${subjectId}/editor/snapshot`
    ) {
      await route.fulfill({ json: snapshot });
      return;
    }
    if (
      method === "PUT" &&
      url.pathname ===
        `/api/subjects/${subjectId}/editor/blocks/${paragraphId}/draft`
    ) {
      await route.fulfill({
        headers: { "Content-Type": "application/json" },
        json: {
          id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
          subject_id: subjectId,
          production_run_id: snapshot.production_run_id,
          occurrence_id: paragraphId,
          base_snapshot_id: snapshot.snapshot_id,
          base_artifact_id: snapshot.synthesis_artifact_id,
          base_artifact_version: snapshot.synthesis_version,
          base_canonical_sha256: snapshot.synthesis_canonical_sha256,
          text_sha256: "b".repeat(64),
          version: 2,
          text: "Texte synthétique privé du brouillon.",
          is_stale: false,
          created_at: "2026-11-20T12:00:00Z",
          updated_at: "2026-11-20T12:00:00Z",
          expires_at: "2026-12-20T12:00:00Z",
        },
      });
      return;
    }
    if (
      method === "DELETE" &&
      url.pathname ===
        `/api/subjects/${subjectId}/editor/blocks/${paragraphId}/draft`
    ) {
      await route.fulfill({ status: 204 });
      return;
    }
    if (method === "GET" && url.pathname === `/api/subjects/${subjectId}`) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
      return;
    }
    const threadPrefix = `/api/subjects/${subjectId}/editor/threads/`;
    if (method === "GET" && url.pathname.startsWith(threadPrefix)) {
      const etag = '"server-draft-e2e-thread"';
      if (route.request().headers()["if-none-match"] === etag) {
        await route.fulfill({ status: 304, headers: { ETag: etag } });
        return;
      }
      const occurrenceId = url.pathname.slice(threadPrefix.length);
      const binding = {
        subject_id: subjectId,
        production_run_id: snapshot.production_run_id,
        occurrence_id: occurrenceId,
        artifact_id: snapshot.synthesis_artifact_id,
        artifact_version: snapshot.synthesis_version,
        canonical_sha256: snapshot.synthesis_canonical_sha256,
      };
      await route.fulfill({
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
      url.pathname ===
        `/api/subjects/${subjectId}/editor/blocks/${paragraphId}/draft`
    ) {
      await route.fulfill({
        headers: { ETag: '"1"' },
        json: {
          id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
          subject_id: subjectId,
          production_run_id: snapshot.production_run_id,
          occurrence_id: paragraphId,
          base_snapshot_id: snapshot.snapshot_id,
          base_artifact_id: snapshot.synthesis_artifact_id,
          base_artifact_version: snapshot.synthesis_version,
          base_canonical_sha256: snapshot.synthesis_canonical_sha256,
          text_sha256: "b".repeat(64),
          version: 1,
          text: "Texte synthétique privé du brouillon.",
          is_stale: false,
          created_at: "2026-11-20T12:00:00Z",
          updated_at: "2026-11-20T12:00:00Z",
          expires_at: "2026-12-20T12:00:00Z",
        },
      });
      return;
    }
    if (method === "GET" && url.pathname.endsWith("/content")) {
      await route.fulfill({ status: 404, body: "" });
      return;
    }
    await route.abort("blockedbyclient");
  });

  const screenshotDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-T-M4-19",
  );
  await mkdir(screenshotDirectory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);
  const paragraph = page.locator(`[data-block-id="${paragraphId}"]`);
  await expect(paragraph).toBeVisible();
  await paragraph.click();
  const status = page.getByText(
    /Brouillon enregistré sur le serveur · expire le/,
  );
  await expect(status).toBeVisible();
  await expect(
    page.getByText("Texte synthétique privé du brouillon."),
  ).toHaveCount(0);
  await page.screenshot({
    path: resolve(screenshotDirectory, "server-draft-1440x900.png"),
  });

  // The local draft field stays collapsed until a draft exists or the
  // reader explicitly opens it.
  await page.getByRole("tab", { name: "Modifier" }).click();
  await page.getByRole("button", { name: "Écrire un brouillon local" }).click();
  const localEditor = page.getByRole("textbox", { name: "Brouillon local" });
  await localEditor.fill("Texte local à remplacer");
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: "Reprendre depuis le serveur" })
    .click();
  await expect(localEditor).toHaveValue(
    "Texte synthétique privé du brouillon.",
  );

  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: "Supprimer le brouillon serveur" })
    .click();
  await expect(
    page.getByText("Aucun brouillon serveur n’est enregistré pour ce bloc."),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Enregistrer le brouillon serveur" })
    .click();
  await expect(status).toBeVisible();
});
