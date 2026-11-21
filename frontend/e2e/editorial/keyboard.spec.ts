import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test, type Locator, type Page } from "@playwright/test";

import type {
  EditorialProposalRead,
  EditorialThreadOpenRead,
  EditorialThreadRead,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const blockId = "22222222-2222-4222-8222-222222222222";
const secondBlockId = "33333333-3333-4333-8333-333333333333";
const proposalId = "44444444-4444-4444-8444-444444444444";
const secondProposalId = "55555555-5555-4555-8555-555555555555";
const threadId = "66666666-6666-4666-8666-666666666666";
const artifactId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const initialSha = "a".repeat(64);
const subjectPath = `/api/subjects/${subjectId}`;
const snapshotPath = `${subjectPath}/editor/snapshot`;
const threadPath = `${subjectPath}/editor/threads/${blockId}`;
const proposalPath = (id: string) => `${threadPath}/proposals/${id}`;

function makeSnapshot(
  text: string,
  version: number,
  discussion = true,
): EditorialWorkspaceSnapshot {
  return {
    subject_id: subjectId,
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    publication_kind: "article",
    synthesis_artifact_id: artifactId,
    synthesis_version: version,
    synthesis_canonical_sha256: version === 1 ? initialSha : "d".repeat(64),
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
        before_hash: initialSha,
        allowed_operations: [
          "rewrite_paragraph",
          ...(discussion ? ["discuss"] : []),
          "insert_after",
          "create_table",
          "create_bar_chart",
          "create_source_figure",
        ],
        revision_status: "ready",
      },
      {
        block_id: secondBlockId,
        kind: "paragraph",
        parent_block_id: null,
        position: 1,
        text: "Synthetic second paragraph.",
        evidence_refs: [],
        chart: null,
        revision_target: { block_id: secondBlockId, legacy_target_id: null },
        before_hash: "c".repeat(64),
        allowed_operations: [],
        revision_status: "ready",
      },
    ],
    enrichments: [],
    document_outline: [],
    capabilities: {
      can_discuss: discussion,
      can_preview_revision: false,
      manual_edit: true,
      can_add_enrichment: true,
      can_preview_pdf: false,
    },
    preview_status: "READY",
    impact_status: "none",
    pdf_status: "CURRENT",
    snapshot_id:
      version === 1
        ? "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        : "ffffffff-ffff-4fff-8fff-ffffffffffff",
  };
}

function makeProposal(
  id: string,
  text: string,
  state: EditorialProposalRead["state"],
  version: number,
): EditorialProposalRead {
  const baseSha = version === 1 ? initialSha : "d".repeat(64);
  return {
    id,
    origin: "bridge_draft",
    subject_id: subjectId,
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    occurrence_id: blockId,
    thread_id: threadId,
    state,
    state_version: state === "proposed" ? 1 : 2,
    operation: "rewrite_paragraph",
    base_artifact_id: artifactId,
    base_artifact_version: version,
    base_canonical_sha256: baseSha,
    base_text: text,
    patch_sha256: "b".repeat(64),
    patch: {
      schema_version: "editorial-patch-v1",
      operation: "rewrite_paragraph",
      target: { block_id: blockId },
      target_id: blockId,
      occurrence_id: blockId,
      before_hash: baseSha,
      rationale: "Synthetic proposal rationale.",
      after: `Synthetic proposed text for ${id}.`,
    },
    diagnostics: {},
    created_at: "2026-11-21T10:00:00Z",
  };
}

function makeProposalThread(
  snapshot: EditorialWorkspaceSnapshot,
): EditorialThreadOpenRead {
  const binding = {
    subject_id: subjectId,
    production_run_id: snapshot.production_run_id,
    occurrence_id: blockId,
    artifact_id: artifactId,
    artifact_version: snapshot.synthesis_version,
    canonical_sha256: snapshot.synthesis_canonical_sha256,
  };
  return {
    kind: "open",
    thread_id: threadId,
    origin: binding,
    current: binding,
    binding_state: "valid",
    previous_sessions: [],
    bridge_status: "ready",
    event_sequence: 2,
    timeline: [proposalId, secondProposalId].map((id, index) => ({
      id: `77777777-7777-4777-8777-77777777777${index}`,
      sequence: index + 1,
      kind: "proposal" as const,
      actor_id: null,
      proposal_id: id,
      created_at: "2026-11-21T10:00:00Z",
      body: null,
    })),
    etag: `"keyboard-thread-${snapshot.synthesis_version}"`,
  };
}

async function tabTo(page: Page, target: Locator) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    await page.keyboard.press("Tab");
    try {
      await expect(target).toBeFocused({ timeout: 100 });
      return;
    } catch {
      // Continue through the document's native tab order.
    }
  }
  throw new Error("Keyboard tab order did not reach the requested control");
}

async function installWorkspaceStubs(
  page: Page,
  options: { discussion?: boolean } = {},
) {
  let snapshot = makeSnapshot(
    "Synthetic original paragraph.",
    1,
    options.discussion ?? true,
  );
  const proposalStates = new Map<string, EditorialProposalRead["state"]>([
    [proposalId, "proposed"],
    [secondProposalId, "proposed"],
  ]);
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
    if (method === "GET" && url.pathname === subjectPath) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
      return;
    }
    if (method === "GET" && url.pathname === `${subjectPath}/content`) {
      await route.fulfill({ status: 404, body: "" });
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
    if (method === "GET" && url.pathname === snapshotPath) {
      await route.fulfill({ json: snapshot });
      return;
    }
    if (
      method === "GET" &&
      url.pathname.startsWith(`${subjectPath}/editor/figures`)
    ) {
      await route.fulfill({
        json: {
          items: [],
          sources: [],
          total: 0,
          available_count: 0,
          offset: 0,
          limit: 30,
        },
      });
      return;
    }
    if (method === "GET" && url.pathname === threadPath) {
      const thread = makeProposalThread(snapshot);
      const etag = route.request().headers()["if-none-match"];
      if (etag === thread.etag) {
        await route.fulfill({ status: 304, headers: { ETag: thread.etag } });
        return;
      }
      const afterSequence = Number(url.searchParams.get("after_sequence") ?? 0);
      await route.fulfill({
        headers: { ETag: thread.etag },
        json: {
          ...thread,
          timeline: thread.timeline.filter(
            (event) => event.sequence > afterSequence,
          ),
        },
      });
      return;
    }
    const requestedProposalId = [proposalId, secondProposalId].find(
      (id) => url.pathname === proposalPath(id),
    );
    if (method === "GET" && requestedProposalId) {
      const text = snapshot.blocks[0]?.text ?? "Synthetic original paragraph.";
      await route.fulfill({
        json: makeProposal(
          requestedProposalId,
          text,
          proposalStates.get(requestedProposalId) ?? "proposed",
          snapshot.synthesis_version,
        ),
      });
      return;
    }
    if (
      method === "POST" &&
      url.pathname === `${subjectPath}/editor/blocks/${blockId}/manual-edit`
    ) {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      snapshot = makeSnapshot(String(body.text), 2, options.discussion ?? true);
      await route.fulfill({
        json: {
          id: proposalId,
          state: "accepted",
          state_version: 2,
          artifact_id: artifactId,
          artifact_version: 2,
          snapshot_id: snapshot.snapshot_id,
          impact_id: "88888888-8888-4888-8888-888888888888",
          impact_state: "pending",
        },
      });
      return;
    }
    const decision = [proposalId, secondProposalId]
      .flatMap((id) => [
        { id, path: `${proposalPath(id)}/accept`, state: "accepted" as const },
        { id, path: `${proposalPath(id)}/reject`, state: "rejected" as const },
      ])
      .find((entry) => entry.path === url.pathname);
    if (method === "POST" && decision) {
      proposalStates.set(decision.id, decision.state);
      await route.fulfill({
        json:
          decision.state === "accepted"
            ? {
                id: decision.id,
                state: "accepted",
                state_version: 2,
                artifact_id: artifactId,
                artifact_version: snapshot.synthesis_version + 1,
                snapshot_id: "99999999-9999-4999-8999-999999999999",
                impact_id: "88888888-8888-4888-8888-888888888888",
                impact_state: "pending",
              }
            : { id: decision.id, state: "rejected", state_version: 2 },
      });
      return;
    }
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });

  return { unexpectedRequests };
}

test("test_keyboard_select_edit_compare_decide_and_restore_focus", async ({
  page,
}) => {
  const { unexpectedRequests } = await installWorkspaceStubs(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const block = page.locator(
    `.editorial-document__block[data-block-id="${blockId}"]`,
  );
  await block.press("Enter");
  await expect(block).toHaveAttribute("data-selected", "true");
  await expect(block).toBeFocused();

  const shotsDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-t-m7-28",
  );
  await mkdir(shotsDirectory, { recursive: true });
  await page.screenshot({
    path: resolve(shotsDirectory, "focus-visible-desktop.png"),
    fullPage: false,
  });

  const editButton = page.getByRole("button", { name: "Modifier" });
  await tabTo(page, editButton);
  await page.keyboard.press("Enter");
  const editor = page.getByRole("textbox", { name: "Texte du bloc" });
  await expect(editor).toBeFocused();
  await editor.fill("Synthetic manually edited paragraph.");
  await editor.press("Control+s");
  await expect(block).toContainText("Synthetic manually edited paragraph.");
  await expect(block).toBeFocused();

  const addButton = page.getByRole("button", { name: "Ajouter après" });
  await tabTo(page, addButton);
  await page.keyboard.press("Enter");
  const insertionMenu = page.getByRole("menu", { name: "Ajouter après" });
  const tableItem = insertionMenu.getByRole("menuitem", { name: "Tableau" });
  const chartItem = insertionMenu.getByRole("menuitem", { name: "Graphique" });
  await expect(tableItem).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(chartItem).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(addButton).toBeFocused();
  await expect(addButton).toHaveAttribute("aria-expanded", "false");

  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "Consigne" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(addButton).toBeFocused();

  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "Consigne" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(addButton).toBeFocused();

  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  const figureDialog = page.getByRole("dialog", {
    name: "Galerie d’images sources",
  });
  await expect(figureDialog).toBeVisible();
  const closeGallery = page.getByRole("button", {
    name: "Fermer la galerie d’images sources",
  });
  await expect(closeGallery).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(addButton).toBeFocused();

  await expect(
    page.getByText("Synthetic proposed text", { exact: false }).first(),
  ).toBeVisible();
  const acceptButton = page.getByRole("button", { name: "Accepter" }).first();
  await tabTo(page, acceptButton);
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Proposition acceptée/i)).toBeVisible();
  await expect(block).toBeFocused();

  const rejectButton = page.getByRole("button", { name: "Rejeter" });
  await tabTo(page, rejectButton);
  await page.keyboard.press("Enter");
  await expect(block).toBeFocused();
  expect(unexpectedRequests).toEqual([]);
});

test("test_async_reply_does_not_steal_focus", async ({ page }) => {
  const { unexpectedRequests } = await installWorkspaceStubs(page);
  const paragraphId = blockId;
  const binding = {
    subject_id: subjectId,
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    occurrence_id: paragraphId,
    artifact_id: artifactId,
    artifact_version: 1,
    canonical_sha256: initialSha,
  };
  const unopened: EditorialThreadRead = {
    kind: "unopened",
    binding,
    current: binding,
    bridge_status: "ready",
    event_sequence: 0,
    timeline: [],
    etag: '"keyboard-thread-empty"',
  };
  const generating: EditorialThreadOpenRead = {
    kind: "open",
    thread_id: threadId,
    origin: binding,
    current: binding,
    binding_state: "valid",
    previous_sessions: [],
    bridge_status: "generating",
    event_sequence: 1,
    timeline: [
      {
        id: "77777777-7777-4777-8777-777777777777",
        sequence: 1,
        kind: "user_message",
        actor_id: null,
        proposal_id: null,
        created_at: "2026-11-21T10:00:00Z",
        body: "Clarifier ce passage.",
      },
    ],
    etag: '"keyboard-thread-generating"',
  };
  const reply: EditorialThreadOpenRead = {
    ...generating,
    bridge_status: "reply_available",
    event_sequence: 2,
    timeline: [
      ...generating.timeline,
      {
        id: "88888888-8888-4888-8888-888888888888",
        sequence: 2,
        kind: "bridge_reply",
        actor_id: null,
        proposal_id: null,
        created_at: "2026-11-21T10:00:02Z",
        body: "Réponse synthétique du Bridge.",
      },
    ],
    etag: '"keyboard-thread-reply"',
  };
  let currentThread: EditorialThreadRead = unopened;
  let replyAvailable = false;
  const threadEntriesPath = `${threadPath}/entries`;

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
    if (method === "GET" && url.pathname === subjectPath) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
      return;
    }
    if (method === "GET" && url.pathname === `${subjectPath}/content`) {
      await route.fulfill({ status: 404, body: "" });
      return;
    }
    if (method === "GET" && url.pathname === snapshotPath) {
      await route.fulfill({
        json: makeSnapshot("Synthetic original paragraph.", 1),
      });
      return;
    }
    if (method === "GET" && url.pathname === threadPath) {
      const current = replyAvailable ? reply : currentThread;
      if (route.request().headers()["if-none-match"] === current.etag) {
        await route.fulfill({ status: 304, headers: { ETag: current.etag } });
        return;
      }
      const afterSequence = Number(url.searchParams.get("after_sequence") ?? 0);
      await route.fulfill({
        headers: { ETag: current.etag },
        json: {
          ...current,
          timeline: current.timeline.filter(
            (event) => event.sequence > afterSequence,
          ),
        },
      });
      return;
    }
    if (method === "POST" && url.pathname === threadEntriesPath) {
      currentThread = generating;
      await route.fulfill({
        status: 202,
        json: {
          kind: "accepted",
          thread_id: threadId,
          entry_id: generating.timeline[0]?.id,
          generation: "queued",
        },
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
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });

  await page.goto(`/subjects/${subjectId}/editor`);
  const block = page.locator(
    `.editorial-document__block[data-block-id="${paragraphId}"]`,
  );
  await block.press("Enter");
  const composer = page.getByRole("textbox", {
    name: "Votre message à Bridge",
  });
  await expect(composer).toBeVisible();
  await expect(composer).toHaveAttribute(
    "aria-describedby",
    "editorial-message-composer-shortcuts",
  );
  await expect(
    page.locator("#editorial-message-composer-shortcuts"),
  ).toContainText("Ctrl/Cmd+Entrée");
  await composer.fill("Clarifier ce passage.");
  await composer.press("Control+Enter");
  await expect(page.getByText("Message envoyé.")).toBeVisible();

  const galleryButton = page.getByRole("button", { name: "Images sources" });
  await page.keyboard.press("Tab");
  await expect(galleryButton).toBeFocused();
  replyAvailable = true;
  await expect(page.getByText("Réponse synthétique du Bridge.")).toBeVisible({
    timeout: 10_000,
  });
  await expect(galleryButton).toBeFocused();
  expect(unexpectedRequests).toEqual([]);
});

test("test_escape_with_unsaved_draft_asks_confirmation_and_restores_focus", async ({
  page,
}) => {
  const { unexpectedRequests } = await installWorkspaceStubs(page, {
    discussion: false,
  });
  await page.goto(`/subjects/${subjectId}/editor`);
  const block = page.locator(
    `.editorial-document__block[data-block-id="${blockId}"]`,
  );
  await block.press("Space");
  await expect(block).toHaveAttribute("data-selected", "true");

  const editButton = page.getByRole("button", { name: "Modifier" });
  await tabTo(page, editButton);
  await page.keyboard.press("Enter");
  const editor = page.getByRole("textbox", { name: "Texte du bloc" });
  await expect(editor).toBeFocused();
  await editor.fill("Unsaved synthetic draft.");

  const confirmationMessages: string[] = [];
  page.on("dialog", async (dialog) => {
    confirmationMessages.push(dialog.message());
    await dialog.dismiss();
  });
  await editor.press("Escape");
  await expect(editor).toHaveValue("Unsaved synthetic draft.");
  await expect(editor).toBeFocused();
  expect(confirmationMessages).toEqual([
    "Abandonner le brouillon local de ce bloc ?",
  ]);

  page.removeAllListeners("dialog");
  page.once("dialog", (dialog) => dialog.accept());
  await editor.press("Escape");
  await expect(editor).toHaveCount(0);
  await expect(block).toBeFocused();
  expect(unexpectedRequests).toEqual([]);
});
