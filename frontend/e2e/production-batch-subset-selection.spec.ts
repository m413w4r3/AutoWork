import { expect, test } from "@playwright/test";

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
  let selectionFetches = 0;
  const subjects = [
    { subject_id: subjectA, title: "Article A", can_start: true },
    { subject_id: subjectB, title: "Article B", can_start: true },
    { subject_id: subjectC, title: "Article C", can_start: true },
    { subject_id: subjectD, title: "Article D", can_start: true },
  ];

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
    if (
      path === `/api/editions/${editionId}/production/batches` &&
      request.method() === "POST"
    ) {
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      productionPostBody = request.postDataJSON();
      batchStarted = true;
      return route.fulfill({ status: 202, json: batchStatus() });
    }
    if (path === `/api/editions/${editionId}/production`)
      return route.fulfill({
        json: {
          edition_id: editionId,
          subjects,
          active_batch: batchStarted ? batchStatus() : null,
          recent_batches: [],
        },
      });
    if (path === `/api/editions/${editionId}/selection`) {
      selectionFetches += 1;
      return route.fulfill({ status: 404, json: {} });
    }
    return route.fulfill({ status: 404, json: {} });
  });

  await page.goto(`/editions/${editionId}/production`);
  expect(selectionFetches).toBe(0);

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

test("Production : board vide retourné en 200", async ({ page }) => {
  const editionId = "23232323-2323-4232-8232-232323232323";
  let boardStatus = 0;

  await page.route("/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/editions/${editionId}`)
      return route.fulfill({
        status: 200,
        json: {
          id: editionId,
          country: "France",
          country_code: "FR",
          period_start: "2026-08-01",
          period_end: "2026-08-31",
          tlp: "GREEN",
          languages: ["fr"],
          state: "open",
          version: 1,
          created_at: "2026-08-29T00:00:00Z",
          updated_at: "2026-08-29T00:00:00Z",
        },
      });
    if (path === `/api/editions/${editionId}/production`) {
      boardStatus = 200;
      return route.fulfill({
        status: boardStatus,
        json: {
          edition_id: editionId,
          subjects: [],
          active_batch: null,
          recent_batches: [],
        },
      });
    }
    return route.fulfill({ status: 404, json: {} });
  });

  await page.goto(`/editions/${editionId}/production`);
  expect(boardStatus).toBe(200);
  await expect(
    page.getByRole("region", { name: "Sélecteur du lot de production" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Démarrer le lot de production" }),
  ).toBeDisabled();
});

test("Production : replay exact et nouvelle clé refusée pendant un batch actif", async ({
  page,
}) => {
  const editionId = "34343434-3434-4343-8343-343434343434";
  const batchId = "e5555555-5555-4555-8555-555555555555";
  const body = { subject_ids: ["a1111111-1111-4111-8111-111111111111"] };
  const firstKey = "aw009-replay-run";
  const secondKey = "aw009-conflicting-run";
  let calls = 0;

  await page.route("/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (
      path === `/api/editions/${editionId}/production/batches` &&
      request.method() === "POST"
    ) {
      calls += 1;
      const key = request.headers()["idempotency-key"];
      expect(request.postDataJSON()).toEqual(body);
      if (key === firstKey)
        return route.fulfill({
          status: calls === 1 ? 202 : 200,
          json: {
            batch_id: batchId,
            status: "running",
            subject_ids: body.subject_ids,
          },
        });
      return route.fulfill({
        status: 409,
        json: { detail: "production_batch_idempotency_conflict" },
      });
    }
    return route.fulfill({ status: 404, json: {} });
  });

  await page.goto("/");
  const result = await page.evaluate(
    async ({ url, payload, first, second }) => {
      const post = (key: string) =>
        fetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": key,
          },
          body: JSON.stringify(payload),
        }).then(async (response) => ({
          status: response.status,
          body: await response.json(),
        }));
      return {
        first: await post(first),
        replay: await post(first),
        conflict: await post(second),
      };
    },
    {
      url: `/api/editions/${editionId}/production/batches`,
      payload: body,
      first: firstKey,
      second: secondKey,
    },
  );

  expect(result.first).toEqual({
    status: 202,
    body: {
      batch_id: batchId,
      status: "running",
      subject_ids: body.subject_ids,
    },
  });
  expect(result.replay).toEqual({
    status: 200,
    body: {
      batch_id: batchId,
      status: "running",
      subject_ids: body.subject_ids,
    },
  });
  expect(result.conflict).toEqual({
    status: 409,
    body: { detail: "production_batch_idempotency_conflict" },
  });
  expect(calls).toBe(3);
});

test("Production : édition archivée en lecture seule", async ({ page }) => {
  const editionId = "45454545-4545-4454-8454-454545454545";

  await page.route("/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === `/api/editions/${editionId}`)
      return route.fulfill({
        json: {
          id: editionId,
          country: "France",
          country_code: "FR",
          period_start: "2026-08-01",
          period_end: "2026-08-31",
          tlp: "GREEN",
          languages: ["fr"],
          state: "archived",
          version: 4,
          created_at: "2026-08-29T00:00:00Z",
          updated_at: "2026-08-29T00:00:00Z",
        },
      });
    if (path === `/api/editions/${editionId}/production`)
      return route.fulfill({
        json: {
          edition_id: editionId,
          subjects: [
            {
              subject_id: "a1111111-1111-4111-8111-111111111111",
              title: "Article archivé",
              can_start: false,
            },
          ],
          active_batch: null,
          recent_batches: [],
        },
      });
    return route.fulfill({ status: 404, json: {} });
  });

  await page.goto(`/editions/${editionId}/production`);
  await expect(
    page.getByRole("button", { name: "Démarrer le lot de production" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("checkbox", { name: "Article archivé" }),
  ).toBeDisabled();
});
