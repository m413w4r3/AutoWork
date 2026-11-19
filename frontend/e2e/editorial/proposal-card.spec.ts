import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import type {
  EditorialProposalRead,
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
  pipeline_generation: 1,
  blocks: [
    {
      block_id: occurrenceId,
      kind: "paragraph",
      parent_block_id: null,
      position: 0,
      text: "Synthetic source paragraph for proposal-card rendering.",
      evidence_refs: [],
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
  snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
};

const proposal: EditorialProposalRead = {
  id: proposalId,
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
  patch_sha256: "b".repeat(64),
  patch: {
    schema_version: "editorial-patch-v1",
    operation: "rewrite_paragraph",
    target: { block_id: occurrenceId },
    target_id: occurrenceId,
    occurrence_id: occurrenceId,
    before_hash: canonicalSha256,
    rationale: "Synthetic display fixture.",
    after: "Synthetic proposed paragraph for visual verification.",
  },
  diagnostics: {},
  created_at: "2026-11-19T12:00:00Z",
};

test("renders a persisted proposal with stubbed API data", async ({ page }) => {
  const unexpectedRequests: string[] = [];
  const proposalPath = `/api/subjects/${subjectId}/editor/threads/${occurrenceId}/proposals/${proposalId}`;
  const threadPath = `/api/subjects/${subjectId}/editor/threads/${occurrenceId}`;
  const snapshotPath = `/api/subjects/${subjectId}/editor/snapshot`;
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
    if (method === "GET" && url.pathname === snapshotPath) {
      await route.fulfill({ json: snapshot });
      return;
    }
    if (method === "GET" && url.pathname === threadPath) {
      const etag = '"synthetic-proposal-thread"';
      if (route.request().headers()["if-none-match"] === etag) {
        await route.fulfill({ status: 304, headers: { ETag: etag } });
        return;
      }
      const binding = {
        subject_id: subjectId,
        production_run_id: snapshot.production_run_id,
        occurrence_id: occurrenceId,
        artifact_id: artifactId,
        artifact_version: 4,
        canonical_sha256: canonicalSha256,
      };
      await route.fulfill({
        status: 200,
        headers: { ETag: etag },
        json: {
          kind: "open",
          thread_id: proposal.thread_id,
          origin: binding,
          current: binding,
          binding_state: "valid",
          previous_sessions: [],
          bridge_status: "reply_available",
          event_sequence: 1,
          timeline: [
            {
              id: "55555555-5555-4555-8555-555555555555",
              sequence: 1,
              kind: "proposal",
              actor_id: null,
              proposal_id: proposalId,
              created_at: proposal.created_at,
              body: null,
            },
          ],
          etag,
        },
      });
      return;
    }
    if (method === "GET" && url.pathname === proposalPath) {
      await route.fulfill({ json: proposal });
      return;
    }
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await page.locator(`[data-block-id="${occurrenceId}"]`).click();
  const card = page.getByRole("article", { name: "Proposition éditoriale" });
  await expect(card).toBeVisible();
  await expect(
    card.getByText("Synthetic proposed paragraph for visual verification."),
  ).toBeVisible();
  await expect(card.getByText("Proposition à consulter")).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Proposer|Accepter|Rejeter/ }),
  ).toHaveCount(0);

  const screenshotDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-m3-12",
  );
  await mkdir(screenshotDirectory, { recursive: true });
  await page.screenshot({
    path: resolve(screenshotDirectory, "proposal-card-1440x900.png"),
    fullPage: true,
  });
  expect(unexpectedRequests).toEqual([]);
});
