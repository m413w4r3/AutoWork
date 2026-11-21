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
const blockId = "22222222-2222-4222-8222-222222222222";

function makeSnapshot(
  text: string,
  version: number,
  impactStatus: EditorialWorkspaceSnapshot["impact_status"],
): EditorialWorkspaceSnapshot {
  return {
    subject_id: subjectId,
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    publication_kind: "article",
    synthesis_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    synthesis_version: version,
    synthesis_canonical_sha256: version === 1 ? "a".repeat(64) : "b".repeat(64),
    enrichment_artifact_id: null,
    enrichment_version: null,
    publication_artifact_id: null,
    publication_version: null,
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
        before_hash: "c".repeat(64),
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
      can_preview_pdf: false,
      can_regenerate_pdf: false,
    },
    preview_status: "READY",
    impact_status: impactStatus,
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
      version === 1
        ? "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        : "ffffffff-ffff-4fff-8fff-ffffffffffff",
  };
}

test("manual edit saves canonically without Bridge and keeps failed text", async ({
  page,
}) => {
  let snapshot = makeSnapshot("Synthetic original paragraph.", 1, "none");
  const manualRequests: Record<string, unknown>[] = [];
  const unexpectedRequests: string[] = [];
  let releaseConflictResponse: () => void = () => {};
  const conflictResponseReleased = new Promise<void>((resolve) => {
    releaseConflictResponse = resolve;
  });
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
    if (method === "GET" && url.pathname.endsWith("/editor/snapshot")) {
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
        headers: { ETag: '"manual-edit-thread"' },
        json: {
          kind: "unopened",
          binding,
          current: binding,
          bridge_status: "ready",
          event_sequence: 0,
          timeline: [],
          etag: '"manual-edit-thread"',
        },
      });
      return;
    }
    if (
      method === "POST" &&
      url.pathname ===
        `/api/subjects/${subjectId}/editor/blocks/${blockId}/manual-edit`
    ) {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      manualRequests.push(body);
      if (body.text === "UnsupportedFamilyX controlled the infected host.") {
        await route.fulfill({
          status: 422,
          json: {
            detail: {
              code: "manual_edit_new_fact_unadmitted",
              message:
                "Cette nouvelle valeur technique n'est pas étayée par les preuves disponibles. Modifiez le passage ou ajoutez une source autorisée.",
            },
          },
        });
        return;
      }
      if (body.text === "Synthetic conflicting revision.") {
        await conflictResponseReleased;
        await route.fulfill({
          status: 409,
          json: {
            detail: {
              code: "manual_edit_stale",
              message: "Le document a changé. Actualisez avant de réessayer.",
            },
          },
        });
        return;
      }
      snapshot = makeSnapshot(String(body.text), 2, "pending");
      await route.fulfill({
        status: 200,
        json: {
          id: "11111111-1111-4111-8111-111111111111",
          state: "accepted",
          state_version: 2,
          artifact_id: snapshot.synthesis_artifact_id,
          artifact_version: 2,
          snapshot_id: snapshot.snapshot_id,
          impact_id: "22222222-2222-4222-8222-222222222223",
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
  const block = page.locator(
    `.editorial-document__block[data-block-id="${blockId}"]`,
  );
  await block.click();
  await page.getByRole("button", { name: "Modifier" }).click();
  const editor = page.getByRole("textbox", { name: "Texte du bloc" });
  await editor.fill("Synthetic manually edited paragraph.");

  const shotsDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-T-M4-20",
  );
  await mkdir(shotsDirectory, { recursive: true });
  await page.screenshot({
    path: resolve(shotsDirectory, "editing-desktop.png"),
    fullPage: true,
  });

  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();
  await expect(block).toContainText("Synthetic manually edited paragraph.");
  await expect(page.getByText("Impact en attente")).toBeVisible();
  await expect(block).toHaveAttribute("data-selected", "true");
  await expect(block).toBeFocused();
  await page.screenshot({
    path: resolve(shotsDirectory, "saved-desktop.png"),
    fullPage: true,
  });

  await page.getByRole("button", { name: "Modifier" }).click();
  await editor.fill("UnsupportedFamilyX controlled the infected host.");
  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();
  await expect(
    page.getByText(/Cette nouvelle valeur technique n'est pas étayée/i),
  ).toBeVisible();
  await expect(editor).toHaveValue(
    "UnsupportedFamilyX controlled the infected host.",
  );
  await page.screenshot({
    path: resolve(shotsDirectory, "unsupported-fact-desktop.png"),
    fullPage: true,
  });

  await editor.fill("Synthetic conflicting revision.");
  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Enregistrement…" }),
  ).toBeVisible();
  await page.screenshot({
    path: resolve(shotsDirectory, "saving-desktop.png"),
    fullPage: true,
  });
  releaseConflictResponse();
  await expect(page.getByRole("alert")).toContainText("Actualisez");
  await expect(editor).toHaveValue("Synthetic conflicting revision.");
  await page.screenshot({
    path: resolve(shotsDirectory, "conflict-desktop.png"),
    fullPage: true,
  });

  expect(manualRequests).toHaveLength(3);
  expect(unexpectedRequests).toEqual([]);
  expect(manualRequests[0]).toMatchObject({
    snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    operation: "rewrite_paragraph",
    text: "Synthetic manually edited paragraph.",
  });
});
