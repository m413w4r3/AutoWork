import { expect, test } from "@playwright/test";

test("Édition : arrêter la production revient à la sélection", async ({
  page,
}) => {
  const editionId = "12121212-1212-4121-8121-121212121212";
  const subjectId = "a1111111-1111-4111-8111-111111111111";
  const batchId = "e5555555-5555-4555-8555-555555555555";
  const runId = "f6666666-6666-4666-8666-666666666666";
  let editionStatus: "production" | "selection" = "production";
  let currentBatchStatus: "running" | "cancelled" = "running";

  const group = {
    id: "g1111111-1111-4111-8111-111111111111",
    edition_id: editionId,
    title: "Article à arrêter",
    outcome: "new_subject",
    status: "selected",
    subject_id: subjectId,
    candidates: [],
    score: {
      impact: 3,
      novelty: 3,
      technical_depth: 3,
      hunting_potential: 3,
      actionability: 3,
      source_quality: 3,
      total: 18,
      justifications: {},
    },
    source_relationship_status: "verified",
    needs_source_verification: false,
    needs_source_expansion: false,
    grouping_confidence: "high",
    grouping_justification: "Sélection canonique.",
    historical_comparison: null,
    version: 1,
  };

  const edition = () => ({
    id: editionId,
    country: "France",
    country_code: "FR",
    period_start: "2026-08-01",
    period_end: "2026-08-31",
    tlp: "GREEN",
    languages: ["fr"],
    target_articles: 1,
    previous_edition_id: null,
    source_profile: "default",
    status: editionStatus,
    version: editionStatus === "production" ? 3 : 4,
    progress_percent: editionStatus === "production" ? 55 : 30,
    allowed_transitions:
      editionStatus === "production" ? ["review"] : ["production", "archived"],
    created_at: "2026-08-29T00:00:00Z",
    updated_at: "2026-08-29T00:00:00Z",
  });

  const board = {
    groups: [group],
    selected_articles: 1,
    ignored: 0,
    undecided: 0,
    target_articles: 1,
    automatic_selection: false,
  };

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
    if (path === `/api/editions/${editionId}/editorial-groups`) {
      await route.fulfill({ json: board });
      return;
    }
    if (
      path === `/api/editions/${editionId}/production/${batchId}/cancel` &&
      request.method() === "POST"
    ) {
      editionStatus = "selection";
      currentBatchStatus = "cancelled";
      await route.fulfill({
        json: {
          action: "cancel",
          batch_id: batchId,
          status: "cancelled",
          edition_status: "selection",
          edition_version: 4,
        },
      });
      return;
    }
    if (path === `/api/editions/${editionId}/production`) {
      await route.fulfill({ json: batch() });
      return;
    }
    await route.fulfill({ status: 404, json: {} });
  });

  await page.goto(`/editions/${editionId}`);
  await expect(
    page.getByRole("button", {
      name: "Arrêter et revenir à la sélection",
    }),
  ).toBeVisible();

  await page
    .getByRole("button", { name: "Arrêter et revenir à la sélection" })
    .click();

  await expect(
    page.getByRole("heading", { name: "1 article éligible" }),
  ).toBeVisible();
  await expect(page.getByText("0 sélectionné pour ce lot")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Sélectionnez au moins un article" }),
  ).toBeDisabled();
});
