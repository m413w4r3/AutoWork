import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import type { EditorialWorkspaceSnapshot } from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const secondSubjectId = "ffffffff-ffff-4fff-8fff-ffffffffffff";
const firstBlockId = "22222222-2222-4222-8222-222222222222";
const secondBlockId = "33333333-3333-4333-8333-333333333333";

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
  pipeline_generation: 1,
  blocks: [
    {
      block_id: firstBlockId,
      kind: "paragraph",
      parent_block_id: null,
      position: 0,
      text: "Synthetic first paragraph for local draft coverage.",
      evidence_refs: [],
      revision_target: { block_id: firstBlockId, legacy_target_id: null },
      before_hash: "b".repeat(64),
      allowed_operations: ["rewrite_paragraph"],
      revision_status: "ready",
    },
    {
      block_id: secondBlockId,
      kind: "paragraph",
      parent_block_id: null,
      position: 1,
      text: "Synthetic second paragraph for local draft coverage.",
      evidence_refs: [],
      revision_target: { block_id: secondBlockId, legacy_target_id: null },
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
  },
  preview_status: "READY",
  impact_status: "none",
  pdf_status: "CURRENT",
  snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
};

const secondSubjectSnapshot: EditorialWorkspaceSnapshot = {
  ...snapshot,
  subject_id: secondSubjectId,
  production_run_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  synthesis_artifact_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  synthesis_canonical_sha256: "f".repeat(64),
  snapshot_id: "11111111-1111-4111-8111-111111111111",
};

async function stubEditorApi(
  page: Page,
  snapshots: EditorialWorkspaceSnapshot[] = [snapshot],
) {
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
    const subjectSnapshot = snapshots.find(
      (candidate) =>
        url.pathname === `/api/subjects/${candidate.subject_id}` ||
        url.pathname.startsWith(`/api/subjects/${candidate.subject_id}/`),
    );
    if (method === "GET" && subjectSnapshot) {
      const subjectPath = `/api/subjects/${subjectSnapshot.subject_id}`;
      if (url.pathname === `${subjectPath}/editor/snapshot`) {
        await route.fulfill({ json: subjectSnapshot });
        return;
      }
      if (url.pathname === subjectPath) {
        await route.fulfill({
          json: {
            ...EDITORIAL_FIXTURES.subject,
            id: subjectSnapshot.subject_id,
          },
        });
        return;
      }
      if (url.pathname === `${subjectPath}/content`) {
        await route.fulfill({ status: 404, body: "" });
        return;
      }
      const threadPrefix = `${subjectPath}/editor/threads/`;
      if (url.pathname.startsWith(threadPrefix)) {
        const etag = '"local-drafts-thread"';
        if (route.request().headers()["if-none-match"] === etag) {
          await route.fulfill({ status: 304, headers: { ETag: etag } });
          return;
        }
        const occurrenceId = url.pathname.slice(threadPrefix.length);
        const binding = {
          subject_id: subjectSnapshot.subject_id,
          production_run_id: subjectSnapshot.production_run_id,
          occurrence_id: occurrenceId,
          artifact_id: subjectSnapshot.synthesis_artifact_id,
          artifact_version: subjectSnapshot.synthesis_version,
          canonical_sha256: subjectSnapshot.synthesis_canonical_sha256,
        };
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
    }
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });
  return unexpectedRequests;
}

test("local drafts stay with their block and remain in browser memory", async ({
  page,
}) => {
  const unexpectedRequests = await stubEditorApi(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const firstBlock = page.locator(
    `.editorial-document__block[data-block-id="${firstBlockId}"]`,
  );
  await firstBlock.click();
  await page.getByRole("button", { name: "Modifier" }).click();
  const editor = page.getByRole("textbox", { name: "Texte du bloc" });
  await expect(editor).toBeFocused();
  await editor.fill("Synthetic unsaved local edit, held only in React memory.");

  const shotsDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-T-M4-18",
  );
  await mkdir(shotsDirectory, { recursive: true });
  await page.screenshot({
    path: resolve(shotsDirectory, "editing-desktop.png"),
    fullPage: true,
  });

  await page
    .locator(`.editorial-document__block[data-block-id="${secondBlockId}"]`)
    .click();
  const plan = page.getByRole("navigation", { name: "Plan éditorial" });
  const resumeDraft = plan.getByRole("button", {
    name: "Reprendre le brouillon : Synthetic first paragraph for local draft coverage.",
  });
  await expect(resumeDraft).toBeVisible();
  await page.screenshot({
    path: resolve(shotsDirectory, "draft-signaled-in-plan-desktop.png"),
    fullPage: true,
  });

  await resumeDraft.click();
  await expect(editor).toHaveValue(
    "Synthetic unsaved local edit, held only in React memory.",
  );
  await expect(
    page.getByRole("button", { name: "Enregistrer", exact: true }),
  ).toHaveCount(1);
  await expect(
    page.getByRole("button", { name: "Annuler", exact: true }),
  ).toBeVisible();
  expect(unexpectedRequests).toEqual([]);
});

test("leaving the workspace asks before discarding a local draft", async ({
  page,
}) => {
  await stubEditorApi(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await page
    .locator(`.editorial-document__block[data-block-id="${firstBlockId}"]`)
    .click();
  await page.getByRole("button", { name: "Modifier" }).click();
  await page.getByRole("textbox", { name: "Texte du bloc" }).fill("Unsaved.");

  let dialogMessage = "";
  page.once("dialog", async (dialog) => {
    expect(dialog.type()).toBe("confirm");
    dialogMessage = dialog.message();
    await dialog.dismiss();
  });
  await page.getByRole("link", { name: /retour au sujet/i }).click();
  expect(dialogMessage).toContain("brouillon local en mémoire");
  await expect(page).toHaveURL(new RegExp(`/subjects/${subjectId}/editor$`));
});

test("keyboard resume closes the desktop intermediate outline before focusing the editor", async ({
  page,
}) => {
  const unexpectedRequests = await stubEditorApi(page);
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await page
    .locator(`.editorial-document__block[data-block-id="${firstBlockId}"]`)
    .click();
  await page.getByRole("button", { name: "Modifier" }).click();
  const editor = page.getByRole("textbox", { name: "Texte du bloc" });
  await editor.fill("Synthetic draft for the intermediate viewport.");

  const planToggle = page.getByRole("button", { name: "Plan", exact: true });
  await planToggle.focus();
  await page.keyboard.press("Enter");
  const plan = page.getByRole("navigation", { name: "Plan éditorial" });
  const resumeDraft = plan.getByRole("button", {
    name: "Reprendre le brouillon : Synthetic first paragraph for local draft coverage.",
  });
  await expect(resumeDraft).toBeVisible();
  const shotsDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-T-M4-18",
  );
  await mkdir(shotsDirectory, { recursive: true });
  await page.screenshot({
    path: resolve(shotsDirectory, "draft-signaled-in-plan-1200x900.png"),
    fullPage: true,
  });

  for (let tab = 0; tab < 4; tab += 1) {
    await page.keyboard.press("Tab");
  }
  await expect(resumeDraft).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator(".editorial-workspace__scrim")).toHaveCount(0);
  await expect(editor).toBeVisible();
  await expect(editor).toBeFocused();
  await expect(editor).toBeInViewport();
  await expect(editor).toHaveValue(
    "Synthetic draft for the intermediate viewport.",
  );
  await page.evaluate(() => {
    const browserWindow = globalThis as unknown as {
      scrollTo: (x: number, y: number) => void;
    };
    browserWindow.scrollTo(0, 0);
  });
  await page.screenshot({
    path: resolve(shotsDirectory, "draft-resumed-1200x900.png"),
    fullPage: true,
  });
  expect(unexpectedRequests).toEqual([]);
});

test("changing subjects remounts the workspace without showing the previous draft", async ({
  page,
}) => {
  const unexpectedRequests = await stubEditorApi(page, [
    snapshot,
    secondSubjectSnapshot,
  ]);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await page
    .locator(`.editorial-document__block[data-block-id="${firstBlockId}"]`)
    .click();
  await page.getByRole("button", { name: "Modifier" }).click();
  await page
    .getByRole("textbox", { name: "Texte du bloc" })
    .fill("Draft belongs to the first subject only.");

  page.once("dialog", async (dialog) => dialog.accept());
  await page.evaluate((path) => {
    const browserWindow = globalThis as unknown as {
      history: {
        pushState: (data: unknown, title: string, url: string) => void;
      };
      dispatchEvent: (event: unknown) => boolean;
      PopStateEvent: new (type: string) => unknown;
    };
    browserWindow.history.pushState({}, "", path);
    browserWindow.dispatchEvent(new browserWindow.PopStateEvent("popstate"));
  }, `/subjects/${secondSubjectId}/editor`);

  await expect(page).toHaveURL(
    new RegExp(`/subjects/${secondSubjectId}/editor$`),
  );
  await expect(
    page.getByRole("button", { name: /Reprendre le brouillon/ }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Draft belongs to the first subject only."),
  ).toHaveCount(0);
  expect(unexpectedRequests).toEqual([]);
});
