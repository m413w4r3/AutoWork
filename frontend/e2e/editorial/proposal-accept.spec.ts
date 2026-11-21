import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import type {
  EditorialProposalRead,
  EditorialThreadRead,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
  fulfillEditorialReadStatuses,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const occurrenceId = "22222222-2222-4222-8222-222222222222";
const proposalId = "33333333-3333-4333-8333-333333333333";
const siblingProposalId = "66666666-6666-4666-8666-666666666666";
const threadId = "44444444-4444-4444-8444-444444444444";
const artifactId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const canonicalSha256 = "a".repeat(64);
const proposalPath = (id: string) =>
  `/api/subjects/${subjectId}/editor/threads/${occurrenceId}/proposals/${id}`;
const threadPath = `/api/subjects/${subjectId}/editor/threads/${occurrenceId}`;
const snapshotPath = `/api/subjects/${subjectId}/editor/snapshot`;
const acceptPath = `${proposalPath(proposalId)}/accept`;

function createSnapshot(
  text: string,
  impactStatus: EditorialWorkspaceSnapshot["impact_status"],
  version: number,
): EditorialWorkspaceSnapshot {
  return {
    subject_id: subjectId,
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    publication_kind: "article",
    synthesis_artifact_id: artifactId,
    synthesis_version: version,
    synthesis_canonical_sha256:
      version === 4 ? canonicalSha256 : "d".repeat(64),
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
        text,
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
      version === 4
        ? "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        : "ffffffff-ffff-4fff-8fff-ffffffffffff",
  };
}

const initialSnapshot = createSnapshot(
  "Synthetic current paragraph in the editorial thread.",
  "none",
  4,
);

function createProposal(
  id: string,
  state: EditorialProposalRead["state"],
): EditorialProposalRead {
  return {
    id,
    origin: "bridge_draft",
    subject_id: subjectId,
    production_run_id: initialSnapshot.production_run_id,
    occurrence_id: occurrenceId,
    thread_id: threadId,
    state,
    state_version: state === "proposed" ? 1 : 2,
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
      after: `Synthetic proposed paragraph ${id}.`,
    },
    diagnostics: {},
    created_at: "2026-11-20T10:00:00Z",
  };
}

function createThread(version: number): EditorialThreadRead {
  const binding = {
    subject_id: subjectId,
    production_run_id: initialSnapshot.production_run_id,
    occurrence_id: occurrenceId,
    artifact_id: artifactId,
    artifact_version: version > 1 ? 5 : 4,
    canonical_sha256: version > 1 ? "d".repeat(64) : canonicalSha256,
  };
  return {
    kind: "open",
    thread_id: threadId,
    origin: binding,
    current: binding,
    binding_state: "valid",
    previous_sessions: [],
    bridge_status: "ready",
    event_sequence: version > 1 ? 3 : 2,
    timeline: [
      {
        id: "55555555-5555-4555-8555-555555555555",
        sequence: 1,
        kind: "proposal",
        actor_id: null,
        proposal_id: proposalId,
        created_at: "2026-11-20T10:00:00Z",
        body: null,
      },
      {
        id: "77777777-7777-4777-8777-777777777777",
        sequence: 2,
        kind: "proposal",
        actor_id: null,
        proposal_id: siblingProposalId,
        created_at: "2026-11-20T10:01:00Z",
        body: null,
      },
    ],
    etag: `"proposal-thread-${version}"`,
  };
}

async function installApiStubs(page: Page, mode: "accept" | "conflict") {
  const unexpectedRequests: string[] = [];
  let accepted = false;
  let conflictRefreshed = false;
  let snapshotReads = 0;
  let threadVersion = 1;
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
      await fulfillEditorialReadStatuses(
        route,
        method,
        url.pathname,
        createSnapshot(
          accepted
            ? "Synthetic accepted paragraph in the current document."
            : "Synthetic current paragraph in the editorial thread.",
          accepted ? "up_to_date" : "none",
          accepted ? 5 : 4,
        ),
      )
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
    if (method === "GET" && url.pathname === snapshotPath) {
      snapshotReads += 1;
      const currentText = accepted
        ? "Synthetic accepted paragraph in the current document."
        : "Synthetic current paragraph in the editorial thread.";
      if (accepted && snapshotReads === 2) {
        await route.fulfill({
          json: createSnapshot(currentText, "pending", 5),
        });
        return;
      }
      await route.fulfill({
        json: createSnapshot(
          currentText,
          accepted ? "up_to_date" : "none",
          accepted ? 5 : 4,
        ),
      });
      return;
    }
    if (method === "GET" && url.pathname === threadPath) {
      const thread = createThread(threadVersion);
      const etag = route.request().headers()["if-none-match"];
      if (etag === thread.etag) {
        await route.fulfill({ status: 304, headers: { ETag: thread.etag } });
        return;
      }
      await route.fulfill({
        status: 200,
        headers: { ETag: thread.etag },
        json: thread,
      });
      return;
    }
    if (method === "GET" && url.pathname === proposalPath(proposalId)) {
      const state = conflictRefreshed
        ? "stale"
        : accepted
          ? "accepted"
          : "proposed";
      await route.fulfill({ json: createProposal(proposalId, state) });
      return;
    }
    if (method === "GET" && url.pathname === proposalPath(siblingProposalId)) {
      await route.fulfill({
        json: createProposal(
          siblingProposalId,
          accepted ? "stale" : "proposed",
        ),
      });
      return;
    }
    if (method === "POST" && url.pathname === acceptPath) {
      expect(route.request().headers()["idempotency-key"]).toMatch(
        /^[A-Za-z0-9_-]{16,128}$/,
      );
      expect(route.request().postDataJSON()).toEqual({
        expected_state_version: 1,
        patch_sha256: "b".repeat(64),
      });
      if (mode === "conflict") {
        await route.fulfill({
          status: 409,
          json: {
            detail: {
              code: "proposal_base_stale",
              message:
                "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition.",
            },
          },
        });
        conflictRefreshed = true;
        return;
      }
      accepted = true;
      threadVersion += 1;
      await route.fulfill({
        json: {
          id: proposalId,
          state: "accepted",
          state_version: 2,
          artifact_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
          artifact_version: 5,
          snapshot_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
          impact_id: "88888888-8888-4888-8888-888888888888",
          impact_state: "pending",
        },
      });
      return;
    }
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });
  return unexpectedRequests;
}

test("compares, accepts, and refreshes the selected desktop document and impact state", async ({
  page,
}) => {
  const unexpectedRequests = await installApiStubs(page, "accept");
  const screenshotDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-T-M3-17b",
  );
  await mkdir(screenshotDirectory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await page.locator(`[data-block-id="${occurrenceId}"]`).click();

  const card = page
    .getByRole("article", { name: "Proposition éditoriale" })
    .first();
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "Comparaison" }).click();
  await expect(page.getByRole("heading", { name: "Discussion" })).toBeVisible();
  await expect(page.getByText(/Discussion sur :/)).toBeVisible();
  await expect(page.getByText("Impact en attente")).toHaveCount(0);

  const accept = card.getByRole("button", { name: "Accepter" });
  await card.getByRole("button", { name: "Rejeter" }).scrollIntoViewIfNeeded();
  await expect(card.getByRole("button", { name: "Comparaison" })).toBeVisible();
  await expect(accept).toBeVisible();
  await expect(card.getByRole("button", { name: "Rejeter" })).toBeVisible();
  await page.screenshot({
    path: resolve(screenshotDirectory, "proposal-actions-1440x900.png"),
  });
  await accept.focus();
  await accept.click();

  await expect(card.getByRole("button", { name: "Acceptée" })).toBeVisible();
  await expect(
    page.locator(`[data-block-id="${occurrenceId}"][data-selected="true"]`),
  ).toContainText("Synthetic accepted paragraph in the current document.");
  await expect(
    page.locator(`[data-block-id="${occurrenceId}"][data-selected="true"]`),
  ).toBeFocused();
  await expect(page.getByRole("heading", { name: "Discussion" })).toBeVisible();
  await expect(page.getByText(/Discussion sur :/)).toBeVisible();
  await expect(page.getByText("Impact en attente")).toBeVisible();
  await expect(page.getByText("Périmée")).toBeVisible();
  await card.getByRole("button", { name: "Acceptée" }).scrollIntoViewIfNeeded();
  await expect(card.getByRole("button", { name: "Comparaison" })).toBeVisible();
  await page.screenshot({
    path: resolve(screenshotDirectory, "proposal-accepted-1440x900.png"),
  });
  await expect(page.getByText("Impact à jour")).toBeVisible({
    timeout: 10_000,
  });
  expect(unexpectedRequests).toEqual([]);
});

test("shows a recoverable French conflict without leaving the selected thread", async ({
  page,
}) => {
  const unexpectedRequests = await installApiStubs(page, "conflict");
  const screenshotDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-T-M3-17b",
  );
  await mkdir(screenshotDirectory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await page.locator(`[data-block-id="${occurrenceId}"]`).click();

  const card = page
    .getByRole("article", { name: "Proposition éditoriale" })
    .first();
  await card.getByRole("button", { name: "Accepter" }).click();
  await expect(
    page.getByText(
      "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Comparer à nouveau" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Demander une variante" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Comparer à nouveau" })
    .scrollIntoViewIfNeeded();
  await expect(card.getByRole("button", { name: "Comparaison" })).toBeVisible();
  await page.screenshot({
    path: resolve(screenshotDirectory, "proposal-accept-conflict-1440x900.png"),
  });

  await page.getByRole("button", { name: "Comparer à nouveau" }).click();
  await expect(card.getByText("Périmée")).toBeVisible();
  await page.getByRole("button", { name: "Demander une variante" }).click();
  await expect(page.getByLabel("Votre message à Bridge")).toBeFocused();
  await expect(
    page.getByRole("button", { name: "Générer une proposition" }),
  ).toBeEnabled();
  await expect(card.getByRole("button", { name: "Accepter" })).toHaveCount(0);
  expect(unexpectedRequests).toEqual([]);
});
