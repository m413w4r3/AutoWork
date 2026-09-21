import { expect, test } from "@playwright/test";

import {
  selectionWireBoard,
  selectionWireItem,
  selectionWireLastDecision,
} from "./support/selectionWire";

test("Édition : sélectionner 2 sujets sur 4 éligibles envoie exactement ce sous-ensemble", async ({
  page,
}) => {
  const editionId = "12121212-1212-4121-8121-121212121212";
  const subjectA = "a1111111-1111-4111-8111-111111111111";
  const subjectB = "b2222222-2222-4222-8222-222222222222";
  const subjectC = "c3333333-3333-4333-8333-333333333333";
  const subjectD = "d4444444-4444-4444-8444-444444444444";
  const batchId = "e5555555-5555-4555-8555-555555555555";
  const runB = "f6666666-6666-4666-8666-666666666666";
  const runD = "f6666666-6666-4666-8666-666666666667";
  let productionPostBody: unknown = null;
  let batchStarted = false;

  const itemFor = (title: string, subjectId: string) =>
    selectionWireItem({
      discovery_subject_id: subjectId,
      title,
      summary: `Résumé de ${title}`,
      artifacts: ["ioc", "configurations"],
      // Only `selected` with a non-null `subject_id` is eligible for a
      // production batch — Selection already materialized the Subject.
      effective_state: "selected",
      subject_id: subjectId,
      last_decision: selectionWireLastDecision({
        id: `decision-${subjectId.slice(0, 4)}`,
        subject_id: subjectId,
      }),
    });

  const selection = selectionWireBoard({
    edition_id: editionId,
    snapshot_id: "99999999-9999-4999-8999-999999999999",
    items: [
      itemFor("Article A", subjectA),
      itemFor("Article B", subjectB),
      itemFor("Article C", subjectC),
      itemFor("Article D", subjectD),
    ],
  });

  const edition = () => ({
    id: editionId,
    country: "Iran",
    country_code: "IR",
    period_start: "2026-08-01",
    period_end: "2026-08-31",
    tlp: "AMBER",
    languages: ["fr"],
    state: "open",
    version: 2,
    created_at: "2026-08-29T00:00:00Z",
    updated_at: "2026-08-29T00:00:00Z",
  });

  const batchStatus = () => ({
    batch_id: batchId,
    edition_id: editionId,
    status: "running",
    phase: "initial",
    next_dispatch_at: null,
    items: 2,
    completed: 0,
    needs_review: 0,
    failed: 0,
    cancelled: 0,
    item_details: [
      {
        position: 1,
        subject_id: subjectB,
        title: "Article B",
        run_id: runB,
        status: "running",
        current_stage: "sources",
        pipeline_generation: 1,
        auto_recovery_count: 0,
        error_code: null,
        error_message: null,
      },
      {
        position: 2,
        subject_id: subjectD,
        title: "Article D",
        run_id: runD,
        status: "queued",
        current_stage: "sources",
        pipeline_generation: 1,
        auto_recovery_count: 0,
        error_code: null,
        error_message: null,
      },
    ],
    created_at: "2026-08-29T00:01:00Z",
    started_at: "2026-08-29T00:02:00Z",
    finished_at: null,
  });

  await page.route("/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === `/api/editions/${editionId}`)
      return route.fulfill({ json: edition() });
    if (path === `/api/editions/${editionId}/selection`)
      return route.fulfill({ json: selection });
    if (
      path === `/api/editions/${editionId}/production` &&
      request.method() === "POST"
    ) {
      productionPostBody = request.postDataJSON();
      batchStarted = true;
      return route.fulfill({ status: 202, json: batchStatus() });
    }
    if (path === `/api/editions/${editionId}/production`)
      return batchStarted
        ? route.fulfill({ json: batchStatus() })
        : route.fulfill({ status: 404, json: {} });
    return route.fulfill({ status: 404, json: {} });
  });

  await page.goto(`/editions/${editionId}/production`);

  // The next-batch selector lives on /production, never on /selection.
  const selector = page.getByRole("region", {
    name: "Sélecteur du lot de production",
  });
  await expect(selector).toBeVisible();
  await expect(selector.getByRole("checkbox")).toHaveCount(4);

  await selector.getByRole("checkbox", { name: "Article B" }).check();
  await selector.getByRole("checkbox", { name: "Article D" }).check();
  await expect(
    selector.getByRole("checkbox", { name: "Article A" }),
  ).not.toBeChecked();
  await expect(
    selector.getByRole("checkbox", { name: "Article C" }),
  ).not.toBeChecked();

  await page
    .getByRole("button", { name: "Démarrer le lot de production" })
    .click();
  await expect(page).toHaveURL(`/editions/${editionId}/production`);
  // Canonical board order, not click order.
  await expect
    .poll(() => productionPostBody)
    .toEqual({ subject_ids: [subjectB, subjectD] });

  await expect(
    page.getByRole("heading", { name: "0 / 2 sujets traités" }),
  ).toBeVisible();
  const tracked = page.getByRole("list", { name: "Suivi des articles" });
  await expect(tracked).toContainText("Article B");
  await expect(tracked).toContainText("Article D");
  await expect(tracked).not.toContainText("Article A");
  await expect(tracked).not.toContainText("Article C");
});
