import { expect, test, type Locator, type Page } from "@playwright/test";

import type {
  EditorialProposalRead,
  EditorialThreadRead,
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
const artifactId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const runId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const initialSha = "a".repeat(64);
const nextSha = "d".repeat(64);
const threadId = "44444444-4444-4444-8444-444444444444";
const proposalId = "33333333-3333-4333-8333-333333333333";
const firstBlockId = "22222222-2222-4222-8222-222222222222";
const secondBlockId = "55555555-5555-4555-8555-555555555555";

interface BlockOptions {
  blockId: string;
  text: string;
  kind?: EditorialWorkspaceBlock["kind"];
  position?: number;
  parentBlockId?: string | null;
  allowedOperations?: string[];
  evidenceRefs?: Record<string, unknown>[];
  beforeHash?: string;
}

function makeBlock({
  blockId,
  text,
  kind = "paragraph",
  position = 0,
  parentBlockId = null,
  allowedOperations = ["rewrite_paragraph"],
  evidenceRefs = [],
  beforeHash = initialSha,
}: BlockOptions): EditorialWorkspaceBlock {
  return {
    block_id: blockId,
    kind,
    parent_block_id: parentBlockId,
    position,
    text,
    evidence_refs: evidenceRefs,
    chart: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: beforeHash,
    allowed_operations: allowedOperations,
    revision_status: "ready",
  };
}

interface SnapshotOptions {
  blocks: EditorialWorkspaceBlock[];
  version?: number;
  previewStatus?: EditorialWorkspaceSnapshot["preview_status"];
  canDiscuss?: boolean;
  manualEdit?: boolean;
  capabilityOverrides?: Partial<EditorialWorkspaceSnapshot["capabilities"]>;
}

function makeSnapshot({
  blocks,
  version = 1,
  previewStatus = "READY",
  canDiscuss = false,
  manualEdit = true,
  capabilityOverrides = {},
}: SnapshotOptions): EditorialWorkspaceSnapshot {
  return {
    subject_id: subjectId,
    production_run_id: runId,
    publication_kind: "article",
    synthesis_artifact_id: artifactId,
    synthesis_version: version,
    synthesis_canonical_sha256: version === 1 ? initialSha : nextSha,
    enrichment_artifact_id: null,
    enrichment_version: null,
    publication_artifact_id: null,
    publication_version: null,
    pipeline_generation: 1,
    blocks,
    enrichments: [],
    document_outline: [],
    capabilities: {
      can_discuss: canDiscuss,
      can_preview_revision: false,
      manual_edit: manualEdit,
      can_add_enrichment: false,
      can_preview_pdf: false,
      can_regenerate_pdf: false,
      ...capabilityOverrides,
    },
    preview_status: previewStatus,
    impact_status: "none",
    pdf_status: previewStatus === "READY" ? "CURRENT" : "STALE",
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

function makeThread(
  occurrenceId: string,
  snapshot: EditorialWorkspaceSnapshot,
  events: EditorialThreadRead["timeline"] = [],
): EditorialThreadRead {
  const binding = {
    subject_id: subjectId,
    production_run_id: runId,
    occurrence_id: occurrenceId,
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
    bridge_status: "reply_available",
    event_sequence: events.length,
    timeline: events,
    etag: '"text-ux-thread"',
  };
}

function proposalEvent(sequence: number) {
  return {
    id: `77777777-7777-4777-8777-77777777777${sequence}`,
    sequence,
    kind: "proposal" as const,
    actor_id: null,
    proposal_id: proposalId,
    created_at: "2026-11-22T10:00:00Z",
    body: null,
  };
}

function makeProposal(
  baseText: string,
  state: EditorialProposalRead["state"] = "proposed",
): EditorialProposalRead {
  return {
    id: proposalId,
    origin: "bridge_draft",
    subject_id: subjectId,
    production_run_id: runId,
    occurrence_id: firstBlockId,
    thread_id: threadId,
    state,
    state_version: 1,
    operation: "rewrite_paragraph",
    base_artifact_id: artifactId,
    base_artifact_version: 1,
    base_canonical_sha256: initialSha,
    base_text: baseText,
    patch_sha256: "b".repeat(64),
    patch: {
      schema_version: "editorial-patch-v1",
      operation: "rewrite_paragraph",
      target: { block_id: firstBlockId },
      target_id: firstBlockId,
      occurrence_id: firstBlockId,
      before_hash: initialSha,
      after: "Synthetic proposed paragraph for the text UX contract.",
      rationale: "Synthetic rationale for the text UX contract.",
      evidence_refs: [
        {
          source_document_id: "12121212-1212-4121-8121-121212121212",
          kind: "fact",
          evidence_key: "f".repeat(64),
        },
      ],
    },
    diagnostics: {},
    created_at: "2026-11-22T10:00:00Z",
  };
}

interface ApiOptions {
  snapshot: () => EditorialWorkspaceSnapshot;
  thread?: (occurrenceId: string) => EditorialThreadRead;
  proposal?: (id: string) => EditorialProposalRead;
  manualEdit?: (body: Record<string, unknown>) => {
    json: unknown;
    status?: number;
    nextSnapshot?: EditorialWorkspaceSnapshot;
  };
  accept?: (body: Record<string, unknown>) => {
    json: unknown;
    nextSnapshot?: EditorialWorkspaceSnapshot;
  };
  reject?: (body: Record<string, unknown>) => { json: unknown };
}

interface ApiCapture {
  manualEditBodies: Record<string, unknown>[];
  bridgeEntryPosts: string[];
  acceptBodies: Record<string, unknown>[];
  unexpected: string[];
}

async function installTextUxApi(
  page: Page,
  options: ApiOptions,
): Promise<ApiCapture> {
  const capture: ApiCapture = {
    manualEditBodies: [],
    bridgeEntryPosts: [],
    acceptBodies: [],
    unexpected: [],
  };
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== EDITORIAL_APP_ORIGIN) {
      capture.unexpected.push(`${method} ${url.origin}${url.pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    const snapshot = options.snapshot();
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
    if (
      await fulfillEditorialReadStatuses(route, method, url.pathname, snapshot)
    ) {
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
    if (method === "GET" && url.pathname === `${subjectPath}/editor/history`) {
      await route.fulfill({
        json: { items: [], next_cursor: null, limit: 20 },
      });
      return;
    }
    const proposalMatch = url.pathname.match(
      /\/editor\/threads\/([^/]+)\/proposals\/([^/]+)$/,
    );
    if (method === "GET" && proposalMatch && options.proposal) {
      await route.fulfill({ json: options.proposal(proposalMatch[2] ?? "") });
      return;
    }
    const acceptMatch = url.pathname.match(
      /\/editor\/threads\/[^/]+\/proposals\/([^/]+)\/(accept|reject)$/,
    );
    if (method === "POST" && acceptMatch) {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      if (acceptMatch[2] === "accept") {
        capture.acceptBodies.push(body);
        if (options.accept) {
          const result = options.accept(body);
          await route.fulfill({ json: result.json });
          return;
        }
      }
      if (acceptMatch[2] === "reject" && options.reject) {
        await route.fulfill({ json: options.reject(body).json });
        return;
      }
    }
    const threadMatch = url.pathname.match(/\/editor\/threads\/([^/]+)$/);
    if (method === "GET" && threadMatch) {
      const occurrenceId = threadMatch[1] ?? "";
      const thread =
        options.thread?.(occurrenceId) ?? unopenedThread(occurrenceId);
      await route.fulfill({ json: thread });
      return;
    }
    if (
      method === "POST" &&
      /\/editor\/threads\/[^/]+\/entries$/.test(url.pathname)
    ) {
      capture.bridgeEntryPosts.push(
        JSON.stringify(route.request().postDataJSON()),
      );
      await route.fulfill({
        status: 202,
        json: {
          kind: "accepted",
          thread_id: threadId,
          entry_id: "99999999-9999-4999-8999-999999999999",
          generation: "queued",
        },
      });
      return;
    }
    if (
      method === "POST" &&
      /\/editor\/blocks\/[^/]+\/manual-edit$/.test(url.pathname)
    ) {
      const body = route.request().postDataJSON() as Record<string, unknown>;
      capture.manualEditBodies.push(body);
      if (options.manualEdit) {
        const result = options.manualEdit(body);
        if (result.nextSnapshot) {
          manualEditSnapshot = result.nextSnapshot;
        }
        await route.fulfill({
          status: result.status ?? 200,
          json: result.json,
        });
        return;
      }
    }
    capture.unexpected.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });

  // Snapshot mutations are kept in this closure so tests never re-stub routes.
  let manualEditSnapshot: EditorialWorkspaceSnapshot | null = null;
  const originalSnapshot = options.snapshot;
  options.snapshot = () => manualEditSnapshot ?? originalSnapshot();
  return capture;
}

function unopenedThread(occurrenceId: string): EditorialThreadRead {
  const binding = {
    subject_id: subjectId,
    production_run_id: runId,
    occurrence_id: occurrenceId,
    artifact_id: artifactId,
    artifact_version: 1,
    canonical_sha256: initialSha,
  };
  return {
    kind: "unopened",
    binding,
    current: binding,
    bridge_status: "ready",
    event_sequence: 0,
    timeline: [],
    etag: '"text-ux-thread-unopened"',
  };
}

function blockElement(page: Page, blockId: string): Locator {
  return page.locator(`.editorial-document__block[data-block-id="${blockId}"]`);
}

async function tabTo(page: Page, target: Locator) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await page.keyboard.press("Tab");
    try {
      await expect(target).toBeFocused({ timeout: 100 });
      return;
    } catch {
      // Continue through the workspace's native tab order.
    }
  }
  throw new Error("Keyboard tab order did not reach the requested control");
}

test("E01 edit and save canonically without a Bridge call and mark the PDF stale", async ({
  page,
}) => {
  let snapshot = makeSnapshot({
    blocks: [
      makeBlock({
        blockId: firstBlockId,
        text: "Synthetic paragraph before manual editing.",
      }),
    ],
  });
  const capture = await installTextUxApi(page, {
    snapshot: () => snapshot,
    manualEdit: (body) => {
      snapshot = makeSnapshot({
        blocks: [
          makeBlock({
            blockId: firstBlockId,
            text: String(body.text),
            beforeHash: nextSha,
          }),
        ],
        version: 2,
        previewStatus: "STALE",
      });
      return {
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
      };
    },
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const block = blockElement(page, firstBlockId);
  await block.click();
  await expect(block).toHaveAttribute("data-selected", "true");
  const toolbar = page.getByRole("toolbar", { name: /Actions du bloc/ });
  await expect(toolbar.getByRole("button", { name: "Modifier" })).toBeVisible();
  await toolbar.getByRole("button", { name: "Modifier" }).click();
  const editor = page.getByRole("textbox", { name: "Texte du bloc" });
  await expect(editor).toBeFocused();
  await editor.fill("Synthetic paragraph after manual editing.");
  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();

  await expect(block).toContainText(
    "Synthetic paragraph after manual editing.",
  );
  await expect(page.getByText("Enregistré · v2")).toBeVisible();
  await expect(page.locator(".editorial-status")).toHaveText("PDF obsolète");
  await expect(block).toHaveAttribute("data-selected", "true");
  await expect(block).toBeFocused();
  expect(capture.manualEditBodies).toEqual([
    {
      snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
      operation: "rewrite_paragraph",
      text: "Synthetic paragraph after manual editing.",
    },
  ]);
  expect(capture.bridgeEntryPosts).toEqual([]);
  expect(capture.unexpected).toEqual([]);
});

test("E01b the lead paragraph saves through rewrite_lead", async ({ page }) => {
  let snapshot = makeSnapshot({
    blocks: [
      makeBlock({
        blockId: firstBlockId,
        kind: "lead_paragraph",
        text: "Synthetic lead before editing.",
        allowedOperations: ["rewrite_lead"],
      }),
    ],
  });
  const capture = await installTextUxApi(page, {
    snapshot: () => snapshot,
    manualEdit: (body) => {
      snapshot = makeSnapshot({
        blocks: [
          makeBlock({
            blockId: firstBlockId,
            kind: "lead_paragraph",
            text: String(body.text),
            allowedOperations: ["rewrite_lead"],
            beforeHash: nextSha,
          }),
        ],
        version: 2,
      });
      return {
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
      };
    },
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await blockElement(page, firstBlockId).click();
  await page.getByRole("button", { name: "Modifier", exact: true }).click();
  const editor = page.getByRole("textbox", { name: "Texte du bloc" });
  await editor.fill("Synthetic lead after editing.");
  await page.getByRole("button", { name: "Enregistrer", exact: true }).click();

  await expect(blockElement(page, firstBlockId)).toContainText(
    "Synthetic lead after editing.",
  );
  expect(capture.manualEditBodies[0]).toMatchObject({
    operation: "rewrite_lead",
    text: "Synthetic lead after editing.",
  });
  expect(capture.unexpected).toEqual([]);
});

test("E02 a local draft survives a block switch and is resumed without a save", async ({
  page,
}) => {
  const snapshot = makeSnapshot({
    blocks: [
      makeBlock({
        blockId: firstBlockId,
        text: "Synthetic first paragraph for draft coverage.",
        position: 0,
      }),
      makeBlock({
        blockId: secondBlockId,
        text: "Synthetic second paragraph for draft coverage.",
        position: 1,
      }),
    ],
  });
  const capture = await installTextUxApi(page, { snapshot: () => snapshot });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const first = blockElement(page, firstBlockId);
  const second = blockElement(page, secondBlockId);
  await first.click();
  await page.getByRole("button", { name: "Modifier", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Texte du bloc" })
    .fill("Synthetic draft held for the first occurrence only.");

  await second.click();
  await expect(first).toContainText("Brouillon local");
  await expect(second).not.toContainText("Brouillon local");
  const outline = page.getByRole("navigation", { name: "Plan éditorial" });
  const resume = outline.getByRole("button", {
    name: "Reprendre le brouillon : Synthetic first paragraph for draft coverage.",
  });
  await expect(resume).toBeVisible();
  await resume.click();
  await expect(
    page.getByRole("textbox", { name: "Texte du bloc" }),
  ).toHaveValue("Synthetic draft held for the first occurrence only.");
  expect(capture.manualEditBodies).toEqual([]);
  const storageLengths = await page.evaluate(() => {
    const browserWindow = globalThis as unknown as {
      localStorage: { length: number };
      sessionStorage: { length: number };
    };
    return {
      local: browserWindow.localStorage.length,
      session: browserWindow.sessionStorage.length,
    };
  });
  expect(storageLengths).toEqual({ local: 0, session: 0 });
  expect(capture.unexpected).toEqual([]);
});

test("E03 a Bridge proposal compares, accepts without a model call, and keeps the block", async ({
  page,
}) => {
  const originalText = "Synthetic current paragraph for the proposal.";
  let snapshot = makeSnapshot({
    blocks: [makeBlock({ blockId: firstBlockId, text: originalText })],
    canDiscuss: true,
  });
  const thread = makeThread(firstBlockId, snapshot, [proposalEvent(1)]);
  let proposalState: EditorialProposalRead["state"] = "proposed";
  const capture = await installTextUxApi(page, {
    snapshot: () => snapshot,
    thread: () => thread,
    proposal: () => makeProposal(originalText, proposalState),
    accept: () => {
      proposalState = "accepted";
      snapshot = makeSnapshot({
        blocks: [
          makeBlock({
            blockId: firstBlockId,
            text: "Synthetic proposed paragraph for the text UX contract.",
            beforeHash: nextSha,
          }),
        ],
        version: 2,
      });
      return {
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
      };
    },
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const block = blockElement(page, firstBlockId);
  await block.click();
  const card = page.getByRole("article", { name: "Proposition éditoriale" });
  await expect(card).toBeVisible();
  await expect(card).toContainText("Bloc ciblé ·");
  await expect(card).toContainText("Base canonique · version 1");
  await expect(card).toContainText(originalText);
  await expect(card).toContainText(
    "Synthetic proposed paragraph for the text UX contract.",
  );
  await expect(card).toContainText("Justification · Synthetic rationale");
  await expect(
    card.getByRole("button", { name: "Demander une variante" }),
  ).toBeVisible();
  await card.getByRole("button", { name: "Différences" }).click();
  const differences = card.getByRole("group", {
    name: "Différences du paragraphe",
  });
  await expect(differences).toContainText("Supprimé ·");
  await expect(differences).toContainText("Ajouté ·");

  await card.getByRole("button", { name: "Accepter" }).click();
  await expect(card.getByRole("button", { name: "Acceptée" })).toBeVisible();
  await expect(block).toContainText(
    "Synthetic proposed paragraph for the text UX contract.",
  );
  await expect(block).toHaveAttribute("data-selected", "true");
  expect(capture.acceptBodies).toHaveLength(1);
  expect(capture.bridgeEntryPosts).toEqual([]);
  expect(capture.unexpected).toEqual([]);
});

test("E04 two identical paragraphs keep independent identities and drafts", async ({
  page,
}) => {
  const sharedText = "Répétition synthétique strictement identique.";
  const snapshot = makeSnapshot({
    blocks: [
      makeBlock({
        blockId: firstBlockId,
        text: sharedText,
        position: 0,
        beforeHash: "b".repeat(64),
      }),
      makeBlock({
        blockId: secondBlockId,
        text: sharedText,
        position: 1,
        beforeHash: "c".repeat(64),
      }),
    ],
  });
  const capture = await installTextUxApi(page, { snapshot: () => snapshot });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const document = page.getByRole("article", { name: "Document éditorial" });
  await expect(document.getByText(sharedText, { exact: true })).toHaveCount(2);
  const first = blockElement(page, firstBlockId);
  const second = blockElement(page, secondBlockId);
  await first.click();
  await expect(first).toHaveAttribute("data-selected", "true");
  await page.getByRole("button", { name: "Modifier", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Texte du bloc" })
    .fill("Draft attached only to the first occurrence.");
  await second.click();
  await expect(second).toHaveAttribute("data-selected", "true");
  await expect(first).not.toHaveAttribute("data-selected", "true");
  await expect(first).toContainText("Brouillon local");
  await expect(second).not.toContainText("Brouillon local");
  expect(capture.manualEditBodies).toEqual([]);
  expect(capture.unexpected).toEqual([]);
});

test("E18 plan navigation scrolls to and keeps the active block, including after history", async ({
  page,
}) => {
  const blocks = Array.from({ length: 14 }, (_, index) =>
    makeBlock({
      blockId: `99999999-9999-4999-8999-9999999999${String(index).padStart(2, "0")}`,
      text: `Synthetic navigation paragraph ${index + 1}.`,
      position: index,
      beforeHash: `${index}`.repeat(64).slice(0, 64),
    }),
  );
  const snapshot = makeSnapshot({ blocks });
  const capture = await installTextUxApi(page, { snapshot: () => snapshot });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const lastBlockId = blocks[blocks.length - 1]?.block_id ?? "";
  const firstBlock = blockElement(page, blocks[0]?.block_id ?? "");
  const lastBlock = blockElement(page, lastBlockId);
  await firstBlock.click();
  await expect(firstBlock).toHaveAttribute("data-selected", "true");
  const scrollBefore = await page.evaluate(
    () => (globalThis as unknown as { scrollY: number }).scrollY,
  );
  const outline = page.getByRole("navigation", { name: "Plan éditorial" });
  await outline
    .getByRole("treeitem", {
      name: /Synthetic navigation paragraph 14\./,
    })
    .click();
  await expect(lastBlock).toHaveAttribute("data-selected", "true");
  await expect(lastBlock).toBeInViewport();
  const scrollAfter = await page.evaluate(
    () => (globalThis as unknown as { scrollY: number }).scrollY,
  );
  expect(scrollAfter).toBeGreaterThan(scrollBefore);

  const disclosure = page.getByRole("button", {
    name: "Historique et détails",
  });
  await disclosure.click();
  await page.getByRole("button", { name: "Historique", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Historique des propositions" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Fermer l’historique" }).click();
  await expect(lastBlock).toHaveAttribute("data-selected", "true");
  await expect(lastBlock).toBeInViewport();
  expect(capture.unexpected).toEqual([]);
});

test("E19 keyboard-only select, edit, save, compare and accept keeps focus", async ({
  page,
}) => {
  const originalText = "Synthetic keyboard paragraph before editing.";
  let snapshot = makeSnapshot({
    blocks: [makeBlock({ blockId: firstBlockId, text: originalText })],
    canDiscuss: true,
  });
  const thread = makeThread(firstBlockId, snapshot, [proposalEvent(1)]);
  let proposalState: EditorialProposalRead["state"] = "proposed";
  const capture = await installTextUxApi(page, {
    snapshot: () => snapshot,
    thread: () => thread,
    proposal: () => makeProposal(originalText, proposalState),
    manualEdit: (body) => {
      snapshot = makeSnapshot({
        blocks: [
          makeBlock({
            blockId: firstBlockId,
            text: String(body.text),
            beforeHash: nextSha,
          }),
        ],
        version: 2,
        canDiscuss: true,
      });
      return {
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
      };
    },
    accept: () => {
      proposalState = "accepted";
      return {
        json: {
          id: proposalId,
          state: "accepted",
          state_version: 2,
          artifact_id: artifactId,
          artifact_version: 3,
          snapshot_id: "99999999-9999-4999-8999-999999999999",
          impact_id: "88888888-8888-4888-8888-888888888888",
          impact_state: "pending",
        },
      };
    },
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/subjects/${subjectId}/editor`);

  const block = blockElement(page, firstBlockId);
  await block.press("Enter");
  await expect(block).toHaveAttribute("data-selected", "true");
  await expect(block).toBeFocused();

  const editButton = page
    .getByRole("toolbar", { name: /Actions du bloc/ })
    .getByRole("button", { name: "Modifier" });
  await tabTo(page, editButton);
  await page.keyboard.press("Enter");
  const editor = page.getByRole("textbox", { name: "Texte du bloc" });
  await expect(editor).toBeFocused();
  await editor.fill("Synthetic keyboard paragraph after editing.");
  await editor.press("Control+s");
  await expect(block).toContainText(
    "Synthetic keyboard paragraph after editing.",
  );
  await expect(block).toBeFocused();
  await expect(page.getByText("Enregistré · v2")).toBeVisible();

  const card = page.getByRole("article", { name: "Proposition éditoriale" });
  await expect(card).toBeVisible();
  const differencesButton = card.getByRole("button", { name: "Différences" });
  await tabTo(page, differencesButton);
  await page.keyboard.press("Enter");
  await expect(
    card.getByRole("group", { name: "Différences du paragraphe" }),
  ).toBeVisible();
  const acceptButton = card.getByRole("button", { name: "Accepter" });
  await tabTo(page, acceptButton);
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Proposition acceptée/i)).toBeVisible();
  await expect(block).toBeFocused();
  expect(capture.manualEditBodies).toHaveLength(1);
  expect(capture.bridgeEntryPosts).toEqual([]);
  expect(capture.unexpected).toEqual([]);
});
