import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test, type Page } from "@playwright/test";

import type {
  EditorialThreadOpenRead,
  EditorialThreadRead,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
} from "./fixtures/editorialApi";

interface ScrollBounds {
  getBoundingClientRect(): { top: number };
}

interface ScrollableBounds extends ScrollBounds {
  scrollTop: number;
}

async function expectMobileInspectorWithinViewport(page: Page) {
  const inspector = page.locator(".editorial-workspace__inspector-column");
  await expect
    .poll(async () => {
      const bounds = await inspector.boundingBox();
      return bounds ? Math.round(bounds.x + bounds.width) : null;
    })
    .toBe(390);
  const bounds = await inspector.boundingBox();
  if (!bounds) throw new Error("Mobile inspector is not rendered");
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(Math.round(bounds.x + bounds.width)).toBeLessThanOrEqual(390);
}

async function positionDesktopDiscussion(page: Page) {
  const inspector = page.locator(".editorial-workspace__inspector-column");
  const heading = page.getByRole("heading", { name: "Discussion" });
  const headingHandle = await heading.elementHandle();
  if (!headingHandle) throw new Error("Discussion heading is not rendered");
  await inspector.evaluate((container, target) => {
    const inspectorElement = container as unknown as ScrollableBounds;
    const headingElement = target as unknown as ScrollBounds;
    const containerBounds = inspectorElement.getBoundingClientRect();
    const targetBounds = headingElement.getBoundingClientRect();
    inspectorElement.scrollTop += targetBounds.top - containerBounds.top - 16;
  }, headingHandle);
}

async function positionMobileDiscussion(page: Page) {
  const content = page.locator(".editorial-inspector__content");
  const heading = page.getByRole("heading", { name: "Discussion" });
  const headingHandle = await heading.elementHandle();
  if (!headingHandle) throw new Error("Discussion heading is not rendered");
  await content.evaluate((scrollArea, target) => {
    const scrollElement = scrollArea as unknown as ScrollableBounds;
    const headingElement = target as unknown as ScrollBounds;
    const scrollAreaBounds = scrollElement.getBoundingClientRect();
    const targetBounds = headingElement.getBoundingClientRect();
    scrollElement.scrollTop += targetBounds.top - scrollAreaBounds.top - 8;
  }, headingHandle);

  const backBounds = await page
    .getByRole("button", { name: "‹ Retour au document" })
    .boundingBox();
  const headingBounds = await heading.boundingBox();
  if (!backBounds || !headingBounds) {
    throw new Error("Mobile inspector header is not visible");
  }
  expect(headingBounds.y).toBeGreaterThanOrEqual(
    backBounds.y + backBounds.height,
  );
}

test("message-only composer renders safe generating, reply and error states", async ({
  page,
}) => {
  test.setTimeout(30_000);
  const subjectId = EDITORIAL_FIXTURES.subject.id;
  const titleId = "00000000-0000-4000-8000-000000000000";
  const sectionId = "11111111-1111-4111-8111-111111111111";
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
        text: "Synthetic editorial article",
        evidence_refs: [],
        revision_target: { block_id: titleId, legacy_target_id: null },
        before_hash: null,
        allowed_operations: [],
        revision_status: "ready",
      },
      {
        block_id: sectionId,
        kind: "section_heading",
        parent_block_id: null,
        position: 1,
        text: "Contexte",
        evidence_refs: [],
        revision_target: { block_id: sectionId, legacy_target_id: null },
        before_hash: null,
        allowed_operations: [],
        revision_status: "ready",
      },
      {
        block_id: paragraphId,
        kind: "paragraph",
        parent_block_id: sectionId,
        position: 2,
        text: "Paragraphe synthétique inchangé.",
        evidence_refs: [],
        revision_target: { block_id: paragraphId, legacy_target_id: null },
        before_hash: null,
        allowed_operations: [],
        revision_status: "ready",
      },
    ],
    enrichments: [],
    document_outline: [
      { block_id: sectionId, label: "Contexte", children: [paragraphId] },
    ],
    capabilities: {
      can_discuss: false,
      can_preview_revision: false,
      manual_edit: false,
      can_add_enrichment: false,
      can_preview_pdf: false,
    },
    preview_status: "READY",
    snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  };
  const binding = {
    subject_id: snapshot.subject_id,
    production_run_id: snapshot.production_run_id,
    occurrence_id: paragraphId,
    artifact_id: snapshot.synthesis_artifact_id,
    artifact_version: snapshot.synthesis_version,
    canonical_sha256: snapshot.synthesis_canonical_sha256,
  };
  const unopened: EditorialThreadRead = {
    kind: "unopened",
    binding,
    current: binding,
    bridge_status: "ready",
    event_sequence: 0,
    timeline: [],
    etag: '"message-thread-empty"',
  };
  const generating: EditorialThreadOpenRead = {
    kind: "open",
    thread_id: "thread-1",
    origin: binding,
    current: binding,
    binding_state: "valid",
    previous_sessions: [],
    bridge_status: "generating",
    event_sequence: 1,
    timeline: [
      {
        id: "entry-1",
        sequence: 1,
        kind: "user_message",
        actor_id: null,
        proposal_id: null,
        created_at: "2026-11-19T10:00:00Z",
        body: "Clarifier le passage.",
      },
    ],
    etag: '"message-thread-generating"',
  };
  const waitingForCapacity: EditorialThreadOpenRead = {
    ...generating,
    bridge_status: "waiting_for_capacity",
    etag: '"message-thread-waiting-for-capacity"',
  };
  const reply: EditorialThreadOpenRead = {
    ...generating,
    bridge_status: "reply_available",
    event_sequence: 2,
    timeline: [
      ...generating.timeline,
      {
        id: "reply-1",
        sequence: 2,
        kind: "bridge_reply",
        actor_id: null,
        proposal_id: null,
        created_at: "2026-11-19T10:00:02Z",
        body: "Réponse de démonstration.",
      },
    ],
    etag: '"message-thread-reply"',
  };
  const reconciliationRequired: EditorialThreadOpenRead = {
    ...reply,
    bridge_status: "reconciliation_required",
    etag: '"message-thread-reconciliation-required"',
  };
  const readyAfterReconciliation: EditorialThreadOpenRead = {
    ...reconciliationRequired,
    bridge_status: "ready",
    event_sequence: 4,
    timeline: [
      ...reconciliationRequired.timeline,
      {
        id: "reconciliation-not-submitted",
        sequence: 4,
        kind: "reconciliation",
        actor_id: null,
        proposal_id: null,
        created_at: "2026-11-19T10:00:04Z",
        body: "Le message n’a pas été reçu par Bridge. Vous pouvez envoyer un nouveau message.",
        reconciliation_outcome: "not_submitted",
      },
    ],
    etag: '"message-thread-not-submitted"',
  };
  const sessionLost: EditorialThreadOpenRead = {
    ...readyAfterReconciliation,
    bridge_status: "session_lost",
    event_sequence: 5,
    timeline: [
      ...readyAfterReconciliation.timeline,
      {
        id: "reconciliation-session-lost",
        sequence: 5,
        kind: "reconciliation",
        actor_id: null,
        proposal_id: null,
        created_at: "2026-11-19T10:00:06Z",
        body: "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre",
        reconciliation_outcome: "session_lost",
      },
    ],
    etag: '"message-thread-session-lost"',
  };
  const newSession: EditorialThreadOpenRead = {
    ...sessionLost,
    thread_id: "thread-2",
    previous_sessions: [1],
    bridge_status: "ready",
    event_sequence: 0,
    timeline: [],
    etag: '"message-thread-session-2"',
  };
  let currentRead: EditorialThreadRead = unopened;
  let sendCount = 0;
  const idempotencyKeys: string[] = [];
  const unexpectedRequests: string[] = [];
  const subjectPath = `/api/subjects/${subjectId}`;
  const snapshotPath = `${subjectPath}/editor/snapshot`;
  const threadPath = `${subjectPath}/editor/threads/${paragraphId}`;
  const screenshotDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-T-M2-09",
  );
  await mkdir(screenshotDirectory, { recursive: true });

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
    if (method === "GET" && url.pathname === snapshotPath) {
      await route.fulfill({ json: snapshot });
      return;
    }
    if (method === "GET" && url.pathname === threadPath) {
      const etag = currentRead.etag;
      if (route.request().headers()["if-none-match"] === etag) {
        await route.fulfill({ status: 304, headers: { ETag: etag } });
        return;
      }
      const afterSequence = Number(
        url.searchParams.get("after_sequence") ?? "0",
      );
      await route.fulfill({
        status: 200,
        headers: { ETag: etag },
        json: {
          ...currentRead,
          timeline: currentRead.timeline.filter(
            (event) => event.sequence > afterSequence,
          ),
        },
      });
      return;
    }
    if (method === "POST" && url.pathname === `${threadPath}/entries`) {
      const idempotencyKey = route.request().headers()["idempotency-key"];
      if (!idempotencyKey) {
        unexpectedRequests.push("POST missing Idempotency-Key");
        await route.fulfill({ status: 400, body: "missing key" });
        return;
      }
      idempotencyKeys.push(idempotencyKey);
      const body = route.request().postDataJSON() as {
        start_new_session?: boolean;
      };
      if (body.start_new_session === true) {
        currentRead = newSession;
        await route.fulfill({
          status: 202,
          json: {
            kind: "session_started",
            thread_id: "thread-2",
            session_number: 2,
          },
        });
        return;
      }
      sendCount += 1;
      if (sendCount === 1) {
        currentRead = waitingForCapacity;
        await route.fulfill({
          status: 202,
          json: {
            kind: "accepted",
            thread_id: "thread-1",
            entry_id: "entry-1",
            generation: "queued",
          },
        });
        return;
      }
      if (sendCount === 2) {
        await route.fulfill({
          status: 403,
          json: {
            detail: {
              code: "external_llm_blocked",
              message: "raw provider or transport detail must not be shown",
            },
          },
        });
        return;
      }
      if (sendCount === 4) {
        currentRead = sessionLost;
        await route.fulfill({
          status: 202,
          json: {
            kind: "accepted",
            thread_id: "thread-1",
            entry_id: "entry-after-reconcile",
            generation: "queued",
          },
        });
        return;
      }
      currentRead = reconciliationRequired;
      await route.fulfill({
        status: 409,
        json: {
          detail: {
            code: "reconciliation_required",
            message: "raw provider or transport detail must not be shown",
          },
        },
      });
      return;
    }
    if (method === "POST" && url.pathname === `${threadPath}/reconcile`) {
      if (!route.request().headers()["idempotency-key"]) {
        unexpectedRequests.push("POST reconcile missing Idempotency-Key");
        await route.fulfill({ status: 400, body: "missing key" });
        return;
      }
      currentRead = readyAfterReconciliation;
      await route.fulfill({
        status: 200,
        json: {
          thread_id: "thread-1",
          outcome: "not_submitted",
          message:
            "Le message n’a pas été reçu par Bridge. Vous pouvez envoyer un nouveau message.",
        },
      });
      return;
    }
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);
  const paragraph = page.locator(`[data-block-id="${paragraphId}"]`);
  await paragraph.click();
  await expect(paragraph).toHaveAttribute("data-selected", "true");
  await page
    .getByRole("textbox", { name: "Votre message à Bridge" })
    .fill("Clarifier le passage.");
  await page.getByRole("button", { name: "Envoyer un message" }).click();
  await expect(
    page.getByText(
      "Votre demande attend qu’une capacité Bridge soit disponible. Son état sera suivi dans ce fil.",
    ),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /réessayer/i })).toHaveCount(0);
  await expect(
    page.locator(".editorial-conversation__announcement"),
  ).toBeFocused();
  await positionDesktopDiscussion(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "waiting-for-capacity-1440x900.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Inspecteur" }).click();
  await expectMobileInspectorWithinViewport(page);
  await positionMobileDiscussion(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "waiting-for-capacity-390x844.png"),
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  currentRead = generating;
  await expect(
    page.getByText(
      "Bridge traite votre demande. L’état sera mis à jour dans ce fil.",
    ),
  ).toBeVisible();
  await positionDesktopDiscussion(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "message-generating-1440x900.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expectMobileInspectorWithinViewport(page);
  await positionMobileDiscussion(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "message-generating-390x844.png"),
  });

  const mobileContent = page.locator(".editorial-inspector__content");
  const mobileScrollTopBeforeReply = await mobileContent.evaluate(
    (element) => (element as unknown as { scrollTop: number }).scrollTop,
  );
  currentRead = reply;
  await expect(
    page.getByText(
      "Une réponse est disponible dans ce fil. Consultez-la avant toute décision.",
    ),
  ).toBeVisible({ timeout: 5_000 });
  await expect(
    page.getByRole("textbox", { name: "Votre message à Bridge" }),
  ).toBeFocused();
  expect(
    await mobileContent.evaluate(
      (element) => (element as unknown as { scrollTop: number }).scrollTop,
    ),
  ).toBe(mobileScrollTopBeforeReply);
  await page.setViewportSize({ width: 1440, height: 900 });
  await positionDesktopDiscussion(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "message-reply-1440x900.png"),
  });
  await expect(page.getByText("Réponse de démonstration.")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expectMobileInspectorWithinViewport(page);
  await positionMobileDiscussion(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "message-reply-390x844.png"),
  });

  await page
    .getByRole("textbox", { name: "Votre message à Bridge" })
    .fill("Une autre clarification.");
  await page.getByRole("button", { name: "Envoyer un message" }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "La politique interdit l’envoi externe de ce contenu.",
  );
  await page.setViewportSize({ width: 1440, height: 900 });
  await positionDesktopDiscussion(page);
  await expect(
    page.getByRole("heading", { name: "Discussion" }),
  ).toBeInViewport();
  await expect(page.getByRole("alert")).toBeInViewport();
  await page.screenshot({
    path: resolve(screenshotDirectory, "message-error-1440x900.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expectMobileInspectorWithinViewport(page);
  await positionMobileDiscussion(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "message-error-390x844.png"),
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page
    .getByRole("textbox", { name: "Votre message à Bridge" })
    .fill("Vérifier l’état de la demande.");
  await page.getByRole("button", { name: "Envoyer un message" }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "L’état de votre envoi est incertain. Consultez le fil avant de continuer.",
  );
  await expect(
    page.getByText(
      "L’état de votre envoi est incertain. Vérifiez son état dans ce fil avant de continuer ; la demande ne sera pas renvoyée automatiquement.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Votre message à Bridge" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Envoyer un message" }),
  ).toBeDisabled();
  await expect(page.getByRole("button", { name: /réessayer/i })).toHaveCount(0);
  await expect(
    page.getByText(
      "L’état de votre envoi est incertain. Vérifiez son état dans ce fil avant de continuer ; la demande ne sera pas renvoyée automatiquement.",
    ),
  ).toBeFocused();
  await positionDesktopDiscussion(page);
  await expect(
    page.getByRole("heading", { name: "Discussion" }),
  ).toBeInViewport();
  await expect(
    page.getByRole("button", { name: "Vérifier l’état de l’envoi" }),
  ).toBeInViewport();
  await page.screenshot({
    path: resolve(screenshotDirectory, "message-uncertain-1440x900.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expectMobileInspectorWithinViewport(page);
  await positionMobileDiscussion(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "message-uncertain-390x844.png"),
  });

  const mobileScrollTopBeforeReconcile = await mobileContent.evaluate(
    (element) => (element as unknown as { scrollTop: number }).scrollTop,
  );
  await page
    .getByRole("button", { name: "Vérifier l’état de l’envoi" })
    .click();
  await expect(
    page
      .locator(".editorial-conversation__announcement")
      .filter({ hasText: "Le message n’a pas été reçu" }),
  ).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Votre message à Bridge" }),
  ).toBeEnabled();
  expect(
    await mobileContent.evaluate(
      (element) => (element as unknown as { scrollTop: number }).scrollTop,
    ),
  ).toBe(mobileScrollTopBeforeReconcile);

  await page.setViewportSize({ width: 1440, height: 900 });
  await page
    .getByRole("textbox", { name: "Votre message à Bridge" })
    .fill("Continuer dans le fil vérifié.");
  await page.getByRole("button", { name: "Envoyer un message" }).click();
  await expect(
    page.getByText(
      "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Démarrer une nouvelle discussion" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /réessayer/i })).toHaveCount(0);
  await positionDesktopDiscussion(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "session-lost-1440x900.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expectMobileInspectorWithinViewport(page);
  await positionMobileDiscussion(page);
  await page.screenshot({
    path: resolve(screenshotDirectory, "session-lost-390x844.png"),
  });
  await page
    .getByRole("button", { name: "Démarrer une nouvelle discussion" })
    .click();
  await expect(page.getByText("Session précédente · 1")).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Votre message à Bridge" }),
  ).toBeEnabled();

  await page.getByRole("button", { name: "Changer de cible" }).click();
  await expect(
    page.locator(".editorial-workspace__inspector-column"),
  ).toHaveAttribute("data-panel-open", "false");
  await expect(paragraph).toBeFocused();

  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(paragraph).toHaveAttribute("data-selected", "true");
  await expect(
    page
      .getByLabel("Document éditorial")
      .getByText("Paragraphe synthétique inchangé.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /générer une proposition/i }),
  ).toHaveCount(0);
  expect(idempotencyKeys).toHaveLength(5);
  expect(new Set(idempotencyKeys).size).toBe(5);
  expect(unexpectedRequests).toEqual([]);
});
