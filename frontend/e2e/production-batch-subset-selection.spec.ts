import { expect, test } from "@playwright/test";

// Reproduces the real bug: a board with more editorially eligible articles
// than the operator wants to produce right now. Only the explicitly checked
// subset must reach the backend and the production console — nothing else.
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

  let productionStarted = false;
  let productionPostBody: unknown = null;

  const groupFor = (title: string, subjectId: string, id: string) => ({
    id,
    edition_id: editionId,
    title,
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
  });

  const board = {
    groups: [
      groupFor("Article A", subjectA, "g-a"),
      groupFor("Article B", subjectB, "g-b"),
      groupFor("Article C", subjectC, "g-c"),
      groupFor("Article D", subjectD, "g-d"),
    ],
    selected_articles: 4,
    ignored: 0,
    undecided: 0,
    target_articles: 4,
    automatic_selection: false,
  };

  const edition = () => ({
    id: editionId,
    country: "Iran",
    country_code: "IR",
    period_start: "2026-08-01",
    period_end: "2026-08-31",
    tlp: "AMBER",
    languages: ["fr"],
    target_articles: 4,
    previous_edition_id: null,
    source_profile: "iran-default",
    status: productionStarted ? "production" : "selection",
    version: productionStarted ? 3 : 2,
    progress_percent: 40,
    allowed_transitions: ["archived"],
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
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === `/api/editions/${editionId}`) {
      await route.fulfill({ json: edition() });
      return;
    }
    if (path === `/api/editions/${editionId}/editorial-groups`) {
      await route.fulfill({ json: board });
      return;
    }
    if (
      path === `/api/editions/${editionId}/production` &&
      request.method() === "POST"
    ) {
      productionStarted = true;
      productionPostBody = request.postDataJSON();
      await route.fulfill({ status: 202, json: batchStatus() });
      return;
    }
    if (path === `/api/editions/${editionId}/production`) {
      await route.fulfill({ json: batchStatus() });
      return;
    }
    await route.fulfill({ status: 404, json: {} });
  });

  await page.goto(`/editions/${editionId}`);
  await expect(
    page.getByRole("heading", { name: "4 articles éligibles" }),
  ).toBeVisible();
  await expect(page.getByText("0 sélectionné pour ce lot")).toBeVisible();

  await page.getByRole("checkbox", { name: "Article B" }).check();
  await page.getByRole("checkbox", { name: "Article D" }).check();
  await expect(page.getByText("2 sélectionnés pour ce lot")).toBeVisible();
  await expect(
    page.getByRole("checkbox", { name: "Article A" }),
  ).not.toBeChecked();
  await expect(
    page.getByRole("checkbox", { name: "Article C" }),
  ).not.toBeChecked();

  await page
    .getByRole("button", { name: "Lancer la production de 2 articles" })
    .click();

  await expect
    .poll(() => productionPostBody)
    .toEqual({ subject_ids: [subjectB, subjectD] });

  await expect(
    page.getByRole("heading", { name: "0 / 2 articles traités" }),
  ).toBeVisible();
  await expect(page.getByText("Article B")).toBeVisible();
  await expect(page.getByText("Article D")).toBeVisible();
  await expect(page.getByText("Article A")).toHaveCount(0);
  await expect(page.getByText("Article C")).toHaveCount(0);
});
