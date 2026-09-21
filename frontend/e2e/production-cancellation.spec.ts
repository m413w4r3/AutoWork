import { expect, test } from "@playwright/test";

import {
  selectionWireBoard,
  selectionWireItem,
  selectionWireLastDecision,
} from "./support/selectionWire";

test("Édition : arrêter la production conserve l’édition ouverte", async ({
  page,
}) => {
  const editionId = "12121212-1212-4121-8121-121212121212";
  const subjectId = "a1111111-1111-4111-8111-111111111111";
  const batchId = "e5555555-5555-4555-8555-555555555555";
  const runId = "f6666666-6666-4666-8666-666666666666";
  let currentBatchStatus: "running" | "cancelled" = "running";
  let batchStarted = false;
  let productionPostBody: unknown = null;
  let selectionFetches = 0;

  const edition = () => ({
    id: editionId,
    country: "France",
    country_code: "FR",
    period_start: "2026-08-01",
    period_end: "2026-08-31",
    tlp: "GREEN",
    languages: ["fr"],
    state: "open",
    version: 3,
    created_at: "2026-08-29T00:00:00Z",
    updated_at: "2026-08-29T00:00:00Z",
  });

  // Selection already materialized exactly one Subject: that is what makes it
  // eligible for a production batch, nothing editorial.
  const selection = selectionWireBoard({
    edition_id: editionId,
    snapshot_id: "99999999-9999-4999-8999-999999999999",
    items: [
      selectionWireItem({
        discovery_subject_id: "d1111111-1111-4111-8111-111111111111",
        title: "Article à arrêter",
        summary: "Résumé de l’article à arrêter.",
        technical_potential: 3,
        technical_potential_reason: "Artefacts annoncés.",
        effective_state: "selected",
        subject_id: subjectId,
        last_decision: selectionWireLastDecision({
          id: "decision-1111",
          subject_id: subjectId,
        }),
      }),
    ],
  });

  const batch = () => ({
    batch_id: batchId,
    edition_id: editionId,
    status: currentBatchStatus,
    phase: "initial",
    next_dispatch_at: null,
    items: 1,
    completed: 0,
    needs_review: 0,
    failed: 0,
    cancelled: currentBatchStatus === "cancelled" ? 1 : 0,
    item_details: [
      {
        position: 1,
        subject_id: subjectId,
        title: "Article à arrêter",
        run_id: runId,
        status: currentBatchStatus === "cancelled" ? "cancelled" : "running",
        current_stage: "sources",
        pipeline_generation: 0,
        auto_recovery_count: 0,
        error_code: null,
        error_message: null,
      },
    ],
    created_at: "2026-08-29T00:01:00Z",
    started_at: "2026-08-29T00:02:00Z",
    finished_at:
      currentBatchStatus === "cancelled" ? "2026-08-29T00:03:00Z" : null,
  });

  await page.route("/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;

    if (path === `/api/editions/${editionId}`) {
      await route.fulfill({ json: edition() });
      return;
    }
    if (path === `/api/editions/${editionId}/selection`) {
      selectionFetches += 1;
      await route.fulfill({ json: selection });
      return;
    }
    if (
      path === `/api/editions/${editionId}/production/${batchId}/cancel` &&
      request.method() === "POST"
    ) {
      currentBatchStatus = "cancelled";
      await route.fulfill({
        json: {
          action: "cancel",
          batch_id: batchId,
          status: "cancelled",
          edition_state: "open",
          edition_version: 4,
        },
      });
      return;
    }
    if (
      path === `/api/editions/${editionId}/production/batches` &&
      request.method() === "POST"
    ) {
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      productionPostBody = request.postDataJSON();
      batchStarted = true;
      await route.fulfill({ status: 202, json: batch() });
      return;
    }
    if (path === `/api/editions/${editionId}/production`) {
      await route.fulfill({
        json: {
          edition_id: editionId,
          subjects: [
            {
              subject_id: subjectId,
              title: "Article à arrêter",
              can_start: true,
            },
          ],
          active_batch: batchStarted ? batch() : null,
          recent_batches: [],
        },
      });
      return;
    }
    await route.fulfill({ status: 404, json: {} });
  });

  // Batch start lives on /production.
  await page.goto(`/editions/${editionId}/production`);
  expect(selectionFetches).toBe(0);
  const selector = page.getByRole("region", {
    name: "Sélecteur du lot de production",
  });
  await selector.getByRole("checkbox", { name: "Article à arrêter" }).check();
  await page
    .getByRole("button", { name: "Démarrer le lot de production" })
    .click();
  await expect
    .poll(() => productionPostBody)
    .toEqual({
      subject_ids: [subjectId],
    });

  const stop = page.getByRole("button", {
    name: "Arrêter le lot de production",
  });
  await expect(stop).toBeVisible();
  await stop.click();

  // Cancelling never closes the edition.
  await expect(stop).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Archiver l’édition" }),
  ).toBeVisible();

  // Selection carries no production control whatsoever.
  await page.getByRole("link", { name: "Sélection" }).click();
  await expect(page).toHaveURL(`/editions/${editionId}/selection`);
  await expect(
    page.getByRole("heading", { name: "Sélection des sujets" }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Sélecteur du lot de production" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: /production/i })).toHaveCount(
    0,
  );
});
