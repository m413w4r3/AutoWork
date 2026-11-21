import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import type {
  EditorialProposalRead,
  EditorialThreadRead,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const occurrenceId = "22222222-2222-4222-8222-222222222222";
const proposalId = "33333333-3333-4333-8333-333333333333";
const artifactId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const canonicalSha256 = "a".repeat(64);

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: subjectId,
  production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  publication_kind: "article",
  synthesis_artifact_id: artifactId,
  synthesis_version: 4,
  synthesis_canonical_sha256: canonicalSha256,
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 2,
  blocks: [
    {
      block_id: occurrenceId,
      kind: "paragraph",
      parent_block_id: null,
      position: 0,
      text: "Synthetic current paragraph in the editorial thread.",
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: occurrenceId, legacy_target_id: null },
      before_hash: canonicalSha256,
      allowed_operations: ["rewrite_paragraph"],
      revision_status: "ready",
    },
  ],
  enrichments: [],
  document_outline: [],
  capabilities: {
    can_discuss: true,
    can_preview_revision: false,
    manual_edit: false,
    can_add_enrichment: false,
    can_preview_pdf: false,
  },
  preview_status: "READY",
  impact_status: "none",
  pdf_status: "CURRENT",
  snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
};

const proposal: EditorialProposalRead = {
  id: proposalId,
  origin: "bridge_draft",
  subject_id: subjectId,
  production_run_id: snapshot.production_run_id,
  occurrence_id: occurrenceId,
  thread_id: "44444444-4444-4444-8444-444444444444",
  state: "proposed",
  state_version: 1,
  operation: "rewrite_paragraph",
  base_artifact_id: artifactId,
  base_artifact_version: 4,
  base_canonical_sha256: canonicalSha256,
  base_text: "Synthetic current paragraph in the editorial thread.",
  patch_sha256: "b".repeat(64),
  patch: {
    schema_version: "editorial-patch-v1",
    operation: "rewrite_paragraph",
    target: { block_id: occurrenceId },
    target_id: occurrenceId,
    occurrence_id: occurrenceId,
    before_hash: canonicalSha256,
    rationale: "Synthetic proposal rationale.",
    after: "Synthetic proposed paragraph shown in the thread.",
  },
  diagnostics: {},
  created_at: "2026-11-20T10:00:00Z",
};

test("generates and displays a persisted proposal in the desktop thread", async ({
  page,
}) => {
  const unexpectedRequests: string[] = [];
  const screenshotDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-T-M3-16",
  );
  const threadPath = `/api/subjects/${subjectId}/editor/threads/${occurrenceId}`;
  const entriesPath = `${threadPath}/entries`;
  const proposalPath = `${threadPath}/proposals/${proposalId}`;
  const snapshotPath = `/api/subjects/${subjectId}/editor/snapshot`;
  const binding = {
    subject_id: subjectId,
    production_run_id: snapshot.production_run_id,
    occurrence_id: occurrenceId,
    artifact_id: artifactId,
    artifact_version: 4,
    canonical_sha256: canonicalSha256,
  };
  let currentRead: EditorialThreadRead = {
    kind: "unopened",
    binding,
    current: binding,
    bridge_status: "ready",
    event_sequence: 0,
    timeline: [],
    etag: '"proposal-thread-unopened"',
  };
  let generationQueued = false;
  let proposalPostCount = 0;
  const proposalRead: EditorialProposalRead = proposal;

  await mkdir(screenshotDirectory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1280 });
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
    if (method === "GET" && url.pathname === `/api/subjects/${subjectId}`) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
      return;
    }
    if (
      method === "GET" &&
      url.pathname === `/api/subjects/${subjectId}/content`
    ) {
      await route.fulfill({ status: 404, body: "" });
      return;
    }
    if (method === "GET" && url.pathname === snapshotPath) {
      await route.fulfill({ json: snapshot });
      return;
    }
    if (method === "POST" && url.pathname === entriesPath) {
      expect(route.request().headers()["idempotency-key"]).toMatch(
        /^[A-Za-z0-9_-]{16,128}$/,
      );
      expect(route.request().postDataJSON()).toEqual({
        kind: "propose",
        text: "Raccourcir ce passage sans perdre les faits établis",
      });
      proposalPostCount += 1;
      generationQueued = true;
      currentRead = {
        kind: "open",
        thread_id: proposal.thread_id ?? "thread-proposal",
        origin: binding,
        current: binding,
        binding_state: "valid",
        previous_sessions: [],
        bridge_status: "generating",
        event_sequence: 1,
        timeline: [
          {
            id: "55555555-5555-4555-8555-555555555555",
            sequence: 1,
            kind: "user_message",
            actor_id: null,
            proposal_id: null,
            created_at: proposal.created_at,
            body: "Raccourcir ce passage sans perdre les faits établis",
          },
        ],
        etag: '"proposal-thread-generating"',
      };
      await route.fulfill({
        status: 202,
        json: {
          kind: "accepted",
          thread_id: currentRead.thread_id,
          entry_id: "55555555-5555-4555-8555-555555555555",
          generation: "queued",
        },
      });
      return;
    }
    if (method === "GET" && url.pathname === threadPath) {
      if (generationQueued && proposalPostCount > 0) {
        currentRead = {
          kind: "open",
          thread_id: proposal.thread_id ?? "thread-proposal",
          origin: binding,
          current: binding,
          binding_state: "valid",
          previous_sessions: [],
          bridge_status: "reply_available",
          event_sequence: 2,
          timeline: [
            ...(currentRead.kind === "open" ? currentRead.timeline : []),
            {
              id: "66666666-6666-4666-8666-666666666666",
              sequence: 2,
              kind: "proposal",
              actor_id: null,
              proposal_id: proposalId,
              created_at: proposal.created_at,
              body: null,
            },
          ],
          etag: '"proposal-thread-complete"',
        };
        generationQueued = false;
      }
      const etag = currentRead.etag;
      if (route.request().headers()["if-none-match"] === etag) {
        await route.fulfill({ status: 304, headers: { ETag: etag } });
        return;
      }
      await route.fulfill({
        status: 200,
        headers: { ETag: etag },
        json: currentRead,
      });
      return;
    }
    if (method === "GET" && url.pathname === proposalPath) {
      await route.fulfill({ json: proposalRead });
      return;
    }
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });

  await page.goto(`/subjects/${subjectId}/editor`);
  await page.locator(`[data-block-id="${occurrenceId}"]`).click();
  await page
    .getByRole("textbox", { name: "Votre message à Bridge" })
    .fill("Raccourcir ce passage sans perdre les faits établis");
  await page.getByRole("button", { name: "Générer une proposition" }).click();

  const card = page.getByRole("article", { name: "Proposition éditoriale" });
  await expect(card).toBeVisible();
  await expect(
    card.getByText("Synthetic proposed paragraph shown in the thread."),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Une réponse est disponible dans ce fil. Consultez-la avant toute décision.",
    ),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Accepter" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Rejeter" })).toBeVisible();
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: resolve(screenshotDirectory, "proposal-thread-1440x1280.png"),
    fullPage: true,
  });
  const actionButtons = page.locator(".editorial-conversation__actions");
  await actionButtons.scrollIntoViewIfNeeded();
  await expect(
    page.getByRole("button", { name: "Envoyer un message" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Générer une proposition" }),
  ).toBeVisible();
  await page.screenshot({
    path: resolve(
      screenshotDirectory,
      "proposal-composer-actions-1440x1280.png",
    ),
    fullPage: false,
  });
  expect(unexpectedRequests).toEqual([]);
});
