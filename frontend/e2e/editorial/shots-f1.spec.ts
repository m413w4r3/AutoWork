import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test, type Locator, type Page } from "@playwright/test";

import type {
  EditorialProposalRead,
  EditorialThreadOpenRead,
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
const sha = "a".repeat(64);
const titleId = "11111111-1111-4111-8111-111111111111";
const headingId = "22222222-2222-4222-8222-222222222222";
const leadId = "33333333-3333-4333-8333-333333333333";
const paragraphId = "44444444-4444-4444-8444-444444444444";
const secondParagraphId = "55555555-5555-4555-8555-555555555555";
const threadId = "66666666-6666-4666-8666-666666666666";
const proposalId = "77777777-7777-4777-8777-777777777777";
const screenshotDirectory = resolve(
  process.cwd(),
  "../.autowork-runs/shots-f1",
);

const paragraphText =
  "Ce paragraphe synthétique décrit un exemple de mise en page éditoriale. Il permet de vérifier la lecture, les actions et les sources associées.";
const proposedText =
  "Ce paragraphe synthétique décrit un exemple de mise en page éditoriale. Il vérifie la lecture, les actions et les sources associées.";

function block(
  blockId: string,
  kind: EditorialWorkspaceSnapshot["blocks"][number]["kind"],
  text: string,
  position: number,
  allowedOperations: string[],
  parentBlockId: string | null = null,
) {
  return {
    block_id: blockId,
    kind,
    parent_block_id: parentBlockId,
    position,
    text,
    evidence_refs:
      kind === "paragraph"
        ? [
            {
              source_title: "Source synthétique",
              passage: "Passage étayant l’occurrence sélectionnée.",
            },
          ]
        : [],
    chart: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: sha,
    allowed_operations: allowedOperations,
    revision_status: "ready" as const,
  };
}

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: subjectId,
  production_run_id: runId,
  publication_kind: "article",
  synthesis_artifact_id: artifactId,
  synthesis_version: 4,
  synthesis_canonical_sha256: sha,
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 2,
  blocks: [
    block(titleId, "title", "Article synthétique", 0, ["rewrite_title"]),
    block(headingId, "section_heading", "Contexte", 1, [
      "rewrite_section",
      "rename_section",
    ]),
    block(leadId, "lead_paragraph", "Chapeau synthétique de l’article.", 2, [
      "rewrite_paragraph",
      "delete_paragraph",
    ]),
    block(
      paragraphId,
      "paragraph",
      paragraphText,
      3,
      [
        "rewrite_paragraph",
        "insert_after",
        "create_table",
        "create_bar_chart",
        "create_source_figure",
      ],
      headingId,
    ),
    block(
      secondParagraphId,
      "paragraph",
      "Un second paragraphe synthétique apporte un peu de longueur au document.",
      4,
      ["rewrite_paragraph"],
      headingId,
    ),
  ],
  enrichments: [],
  document_outline: [
    {
      block_id: headingId,
      label: "Contexte",
      children: [paragraphId, secondParagraphId],
    },
  ],
  capabilities: {
    can_discuss: true,
    can_preview_revision: false,
    manual_edit: true,
    can_add_enrichment: true,
    can_preview_pdf: false,
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

const binding = {
  subject_id: subjectId,
  production_run_id: runId,
  occurrence_id: paragraphId,
  artifact_id: artifactId,
  artifact_version: 4,
  canonical_sha256: sha,
};

const thread: EditorialThreadOpenRead = {
  kind: "open",
  thread_id: threadId,
  origin: binding,
  current: binding,
  binding_state: "valid",
  previous_sessions: [],
  bridge_status: "reply_available",
  event_sequence: 3,
  timeline: [
    {
      id: "88888888-8888-4888-8888-888888888881",
      sequence: 1,
      kind: "user_message",
      actor_id: null,
      proposal_id: null,
      created_at: "2026-11-22T10:00:00Z",
      body: "Raccourcis ce passage sans perdre les faits établis.",
    },
    {
      id: "88888888-8888-4888-8888-888888888882",
      sequence: 2,
      kind: "bridge_reply",
      actor_id: null,
      proposal_id: null,
      created_at: "2026-11-22T10:00:20Z",
      body: "Voici une proposition qui conserve les faits documentés.",
    },
    {
      id: "88888888-8888-4888-8888-888888888883",
      sequence: 3,
      kind: "proposal",
      actor_id: null,
      proposal_id: proposalId,
      created_at: "2026-11-22T10:01:00Z",
      body: null,
    },
  ],
  etag: '"shots-f1-thread"',
};

const proposal: EditorialProposalRead = {
  id: proposalId,
  origin: "bridge_draft",
  subject_id: subjectId,
  production_run_id: runId,
  occurrence_id: paragraphId,
  thread_id: threadId,
  state: "proposed",
  state_version: 1,
  operation: "rewrite_paragraph",
  base_artifact_id: artifactId,
  base_artifact_version: 4,
  base_canonical_sha256: sha,
  base_text: paragraphText,
  patch_sha256: "b".repeat(64),
  patch: {
    schema_version: "editorial-patch-v1",
    operation: "rewrite_paragraph",
    target: { block_id: paragraphId },
    target_id: paragraphId,
    occurrence_id: paragraphId,
    before_hash: sha,
    after: proposedText,
    rationale: "Réécriture plus concise sans retirer de fait documenté.",
  },
  diagnostics: {},
  created_at: "2026-11-22T10:01:00Z",
};

async function installStubs(page: Page) {
  const unexpected: string[] = [];
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== EDITORIAL_APP_ORIGIN) {
      unexpected.push(`${method} ${url.origin}${url.pathname}`);
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
    if (
      method === "GET" &&
      url.pathname === `${subjectPath}/editor/threads/${paragraphId}`
    ) {
      await route.fulfill({ json: thread });
      return;
    }
    if (
      method === "GET" &&
      url.pathname ===
        `${subjectPath}/editor/threads/${paragraphId}/proposals/${proposalId}`
    ) {
      await route.fulfill({ json: proposal });
      return;
    }
    if (method === "GET" && /\/editor\/threads\/[^/]+$/.test(url.pathname)) {
      const occurrenceId = url.pathname.split("/").pop() ?? "";
      await route.fulfill({
        json: {
          kind: "unopened",
          binding: { ...binding, occurrence_id: occurrenceId },
          current: { ...binding, occurrence_id: occurrenceId },
          bridge_status: "ready",
          event_sequence: 0,
          timeline: [],
          etag: '"shots-f1-empty"',
        },
      });
      return;
    }
    unexpected.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });
  return unexpected;
}

function paragraphBlock(page: Page): Locator {
  return page.locator(
    `.editorial-document__block[data-block-id="${paragraphId}"]`,
  );
}

async function prepareState(page: Page, state: string) {
  const paragraph = paragraphBlock(page);
  if (state === "initial") return;
  await paragraph.click();
  await expect(paragraph).toHaveAttribute("data-selected", "true");
  if (state === "selected") return;
  if (state === "inline-edit") {
    await page.getByRole("button", { name: "Modifier", exact: true }).click();
    await expect(
      page.getByRole("textbox", { name: "Texte du bloc" }),
    ).toBeVisible();
    return;
  }
  if (state === "draft") {
    await page.getByRole("button", { name: "Modifier", exact: true }).click();
    await page
      .getByRole("textbox", { name: "Texte du bloc" })
      .fill("Brouillon local conservé pour ce bloc synthétique.");
    await page
      .locator(
        `.editorial-document__block[data-block-id="${secondParagraphId}"]`,
      )
      .click();
    await expect(paragraph).toContainText("Brouillon local");
    return;
  }
  if (state === "discussion") {
    await expect(
      page.getByRole("textbox", { name: "Votre message à Bridge" }),
    ).toBeVisible();
    return;
  }
  if (state === "proposal-diff") {
    const card = page.getByRole("article", { name: "Proposition éditoriale" });
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "Différences" }).click();
    await expect(
      card.getByRole("group", { name: "Différences du paragraphe" }),
    ).toBeVisible();
    return;
  }
  throw new Error(`Unknown state ${state}`);
}

test("capture F1 text UX states at the desktop widths", async ({ page }) => {
  test.setTimeout(180_000);
  await mkdir(screenshotDirectory, { recursive: true });
  const viewports = [
    { width: 1024, height: 800 },
    { width: 1280, height: 900 },
    { width: 1440, height: 1000 },
    { width: 1920, height: 1080 },
  ];
  const states = [
    "initial",
    "selected",
    "inline-edit",
    "draft",
    "discussion",
    "proposal-diff",
  ];
  const failures: string[] = [];
  for (const viewport of viewports) {
    for (const state of states) {
      const scenarioPage = await page.context().newPage();
      try {
        scenarioPage.setDefaultTimeout(8_000);
        const unexpected = await installStubs(scenarioPage);
        await scenarioPage.setViewportSize(viewport);
        await scenarioPage.goto(`/subjects/${subjectId}/editor`);
        await expect(
          scenarioPage.locator(".editorial-workspace__grid"),
        ).toBeVisible();
        await prepareState(scenarioPage, state);
        const name = `${state}-${viewport.width}x${viewport.height}.png`;
        await scenarioPage.screenshot({
          path: resolve(screenshotDirectory, name),
        });
        const noHorizontalOverflow = Boolean(
          await scenarioPage.evaluate(
            "document.scrollingElement && document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth",
          ),
        );
        if (!noHorizontalOverflow) {
          failures.push(`${name}: horizontal page overflow`);
        }
        if (unexpected.length > 0) {
          failures.push(
            `${name}: unexpected requests ${unexpected.join(", ")}`,
          );
        }
      } finally {
        await scenarioPage.close();
      }
    }
  }
  expect(failures, failures.join("\n")).toEqual([]);
});
