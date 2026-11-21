import { expect, test } from "@playwright/test";

import type {
  EditorialProposalRead,
  EditorialThreadOpenRead,
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
const firstProposalId = "33333333-3333-4333-8333-333333333333";
const secondProposalId = "66666666-6666-4666-8666-666666666666";
const threadId = "44444444-4444-4444-8444-444444444444";
const artifactId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const canonicalSha256 = "a".repeat(64);
const snapshotPath = `/api/subjects/${subjectId}/editor/snapshot`;
const threadPath = `/api/subjects/${subjectId}/editor/threads/${occurrenceId}`;
const proposalPath = (proposalId: string) =>
  `${threadPath}/proposals/${proposalId}`;
const acceptPath = `${proposalPath(secondProposalId)}/accept`;

function createSnapshot(accepted: boolean): EditorialWorkspaceSnapshot {
  return {
    subject_id: subjectId,
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    publication_kind: "article",
    synthesis_artifact_id: artifactId,
    synthesis_version: accepted ? 5 : 4,
    synthesis_canonical_sha256: accepted ? "d".repeat(64) : canonicalSha256,
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
        text: accepted
          ? "Synthetic accepted second variant."
          : "Synthetic current paragraph in the editorial thread.",
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
    impact_status: accepted ? "pending" : "none",
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
    snapshot_id: accepted
      ? "ffffffff-ffff-4fff-8fff-ffffffffffff"
      : "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  };
}

function createProposal(
  proposalId: string,
  state: EditorialProposalRead["state"],
): EditorialProposalRead {
  const isSecond = proposalId === secondProposalId;
  const after = isSecond
    ? "Synthetic accepted second variant."
    : "Synthetic first variant for comparison.";
  return {
    id: proposalId,
    origin: "bridge_draft",
    subject_id: subjectId,
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    occurrence_id: occurrenceId,
    thread_id: threadId,
    state,
    state_version: state === "proposed" ? 1 : 2,
    operation: "rewrite_paragraph",
    base_artifact_id: artifactId,
    base_artifact_version: 4,
    base_canonical_sha256: canonicalSha256,
    base_text: "Synthetic current paragraph in the editorial thread.",
    patch_sha256: isSecond ? "c".repeat(64) : "b".repeat(64),
    patch: {
      schema_version: "editorial-patch-v1",
      operation: "rewrite_paragraph",
      target: { block_id: occurrenceId },
      target_id: occurrenceId,
      occurrence_id: occurrenceId,
      before_hash: canonicalSha256,
      rationale: "Synthetic proposal rationale.",
      after,
    },
    diagnostics: {},
    created_at: isSecond ? "2026-11-21T10:02:00Z" : "2026-11-21T10:01:00Z",
  };
}

function createThread(accepted: boolean): EditorialThreadOpenRead {
  const origin = {
    subject_id: subjectId,
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    occurrence_id: occurrenceId,
    artifact_id: artifactId,
    artifact_version: 4,
    canonical_sha256: canonicalSha256,
  };
  const current = accepted
    ? { ...origin, artifact_version: 5, canonical_sha256: "d".repeat(64) }
    : origin;
  const timeline: EditorialThreadOpenRead["timeline"] = [
    {
      id: "10000000-0000-4000-8000-000000000001",
      sequence: 1,
      kind: "user_message",
      actor_id: null,
      proposal_id: null,
      created_at: "2026-11-21T10:00:00Z",
      body: "Propose deux variantes de réécriture.",
    },
    {
      id: "10000000-0000-4000-8000-000000000002",
      sequence: 2,
      kind: "bridge_reply",
      actor_id: null,
      proposal_id: null,
      created_at: "2026-11-21T10:00:20Z",
      body: "Deux variantes sont disponibles pour comparaison.",
    },
    {
      id: "10000000-0000-4000-8000-000000000003",
      sequence: 3,
      kind: "proposal",
      actor_id: null,
      proposal_id: firstProposalId,
      created_at: "2026-11-21T10:01:00Z",
      body: null,
    },
    {
      id: "10000000-0000-4000-8000-000000000004",
      sequence: 4,
      kind: "user_message",
      actor_id: null,
      proposal_id: null,
      created_at: "2026-11-21T10:01:20Z",
      body: "Je préfère la deuxième.",
    },
    {
      id: "10000000-0000-4000-8000-000000000005",
      sequence: 5,
      kind: "bridge_reply",
      actor_id: null,
      proposal_id: null,
      created_at: "2026-11-21T10:01:40Z",
      body: "La deuxième variante conserve le fait documenté.",
    },
    {
      id: "10000000-0000-4000-8000-000000000006",
      sequence: 6,
      kind: "proposal",
      actor_id: null,
      proposal_id: secondProposalId,
      created_at: "2026-11-21T10:02:00Z",
      body: null,
    },
    {
      id: "10000000-0000-4000-8000-000000000007",
      sequence: 7,
      kind: "user_message",
      actor_id: null,
      proposal_id: null,
      created_at: "2026-11-21T10:02:20Z",
      body: "Garde cette version et raccourcis-la légèrement.",
    },
    {
      id: "10000000-0000-4000-8000-000000000008",
      sequence: 8,
      kind: "bridge_reply",
      actor_id: null,
      proposal_id: null,
      created_at: "2026-11-21T10:02:40Z",
      body: "La version retenue reste liée à ce fil.",
    },
  ];
  if (accepted) {
    timeline.push({
      id: "10000000-0000-4000-8000-000000000009",
      sequence: 9,
      kind: "accepted",
      actor_id: null,
      proposal_id: secondProposalId,
      created_at: "2026-11-21T10:03:00Z",
      body: null,
    });
  }
  return {
    kind: "open",
    thread_id: threadId,
    origin,
    current,
    binding_state: "valid",
    previous_sessions: [],
    bridge_status: "reply_available",
    event_sequence: accepted ? 9 : 8,
    timeline,
    etag: accepted ? '"ux-three-turns-accepted"' : '"ux-three-turns-open"',
  };
}

test("three Bridge turns retain the selected second variant after acceptance", async ({
  page,
}) => {
  let accepted = false;
  const proposalReads = new Set<string>();
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
      await fulfillEditorialReadStatuses(
        route,
        method,
        url.pathname,
        createSnapshot(accepted),
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
      await route.fulfill({ json: createSnapshot(accepted) });
      return;
    }
    if (method === "GET" && url.pathname === threadPath) {
      const thread: EditorialThreadRead = createThread(accepted);
      const etag = route.request().headers()["if-none-match"];
      if (etag === thread.etag) {
        await route.fulfill({ status: 304, headers: { ETag: thread.etag } });
        return;
      }
      const afterSequence = Number(
        url.searchParams.get("after_sequence") ?? "0",
      );
      await route.fulfill({
        status: 200,
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
    if (
      method === "GET" &&
      (url.pathname === proposalPath(firstProposalId) ||
        url.pathname === proposalPath(secondProposalId))
    ) {
      const id = url.pathname.endsWith(firstProposalId)
        ? firstProposalId
        : secondProposalId;
      proposalReads.add(id);
      await route.fulfill({
        json: createProposal(
          id,
          accepted
            ? id === secondProposalId
              ? "accepted"
              : "stale"
            : "proposed",
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
        patch_sha256: "c".repeat(64),
      });
      accepted = true;
      await route.fulfill({
        json: {
          id: secondProposalId,
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

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);
  await page.locator(`[data-block-id="${occurrenceId}"]`).click();

  const proposalCards = page.getByRole("article", {
    name: "Proposition éditoriale",
  });
  await expect(proposalCards).toHaveCount(2);
  const firstCard = proposalCards.nth(0);
  const secondCard = proposalCards.nth(1);
  await expect(
    firstCard.getByText("Synthetic first variant for comparison."),
  ).toBeVisible();
  await expect(
    secondCard.getByText("Synthetic accepted second variant."),
  ).toBeVisible();
  await expect(
    page.getByText("Je préfère la deuxième.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Garde cette version et raccourcis-la légèrement.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect.poll(() => proposalReads.has(secondProposalId)).toBe(true);

  await secondCard.getByRole("button", { name: "Accepter" }).click();
  await expect(
    secondCard.getByRole("button", { name: "Acceptée" }),
  ).toBeVisible();
  await expect(firstCard.getByRole("button", { name: "Acceptée" })).toHaveCount(
    0,
  );
  await expect(
    page.locator(`[data-block-id="${occurrenceId}"][data-selected="true"]`),
  ).toContainText("Synthetic accepted second variant.");
  await expect(page.getByRole("heading", { name: "Discussion" })).toBeVisible();
  await expect(
    page.getByText("Je préfère la deuxième.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("La version retenue reste liée à ce fil.", { exact: true }),
  ).toBeVisible();
  expect(proposalReads.has(firstProposalId)).toBe(true);
  expect(unexpectedRequests).toEqual([]);
});

const duplicateText = "Répétition synthétique identique.";
const firstDuplicateId = "77777777-7777-4777-8777-777777777777";
const secondDuplicateId = "88888888-8888-4888-8888-888888888888";

function createDuplicateParagraphSnapshot(): EditorialWorkspaceSnapshot {
  const block = (blockId: string, position: number, beforeHash: string) => ({
    block_id: blockId,
    kind: "paragraph" as const,
    parent_block_id: null,
    position,
    text: duplicateText,
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: beforeHash,
    allowed_operations: ["rewrite_paragraph"],
    revision_status: "ready" as const,
  });
  return {
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
      block(firstDuplicateId, 0, "b".repeat(64)),
      block(secondDuplicateId, 1, "c".repeat(64)),
    ],
    enrichments: [],
    document_outline: [],
    capabilities: {
      can_discuss: false,
      can_preview_revision: false,
      manual_edit: false,
      can_add_enrichment: false,
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
    snapshot_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  };
}

test("two identical paragraphs stay distinct selectable occurrences", async ({
  page,
}) => {
  const snapshot = createDuplicateParagraphSnapshot();
  const unexpectedRequests: string[] = [];
  const subjectPath = `/api/subjects/${subjectId}`;
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
      await route.fulfill({ json: snapshot });
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
    const threadPrefix = `${subjectPath}/editor/threads/`;
    const occurrence = url.pathname.startsWith(threadPrefix)
      ? url.pathname.slice(threadPrefix.length)
      : null;
    if (
      method === "GET" &&
      (occurrence === firstDuplicateId || occurrence === secondDuplicateId)
    ) {
      const etag = '"duplicate-occurrence-thread"';
      const binding = {
        subject_id: snapshot.subject_id,
        production_run_id: snapshot.production_run_id,
        occurrence_id: occurrence,
        artifact_id: snapshot.synthesis_artifact_id,
        artifact_version: snapshot.synthesis_version,
        canonical_sha256: snapshot.synthesis_canonical_sha256,
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
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);
  const document = page.getByRole("article", { name: "Document éditorial" });
  await expect(document.getByText(duplicateText, { exact: true })).toHaveCount(
    2,
  );
  const first = page.locator(`[data-block-id="${firstDuplicateId}"]`);
  const second = page.locator(`[data-block-id="${secondDuplicateId}"]`);
  await first.click();
  await expect(first).toHaveAttribute("data-selected", "true");
  await expect(second).toHaveAttribute("data-selected", "false");
  await second.click();
  await expect(second).toHaveAttribute("data-selected", "true");
  await expect(first).toHaveAttribute("data-selected", "false");
  expect(unexpectedRequests).toEqual([]);
});
