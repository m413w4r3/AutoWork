import { expect, test } from "@playwright/test";

import { EDITORIAL_FIXTURES } from "../../e2e/editorial/fixtures/editorialApi";

test("real-backend-lite loads the shared synthetic subject", async ({
  page,
}) => {
  test.skip(
    process.env.EDITORIAL_PLAYWRIGHT_MODE !== "real-backend-lite",
    "Set EDITORIAL_PLAYWRIGHT_MODE=real-backend-lite to select this suite.",
  );
  test.skip(
    process.env.EDITORIAL_REAL_LITE_ATTESTED_FAKE_PROVIDER !== "1",
    "Skipped: operator must attest a PostgreSQL test backend, fake provider, and no live provider keys with EDITORIAL_REAL_LITE_ATTESTED_FAKE_PROVIDER=1.",
  );

  const health = await page.request.get("/api/health");
  expect(
    health.status(),
    "the loopback API health endpoint must be reachable",
  ).toBe(200);

  const subjectId = EDITORIAL_FIXTURES.subject.id;
  const response = await page.request.get(`/api/subjects/${subjectId}`);
  expect(response.status()).toBe(200);
  const subject = (await response.json()) as typeof EDITORIAL_FIXTURES.subject;
  expect(subject.title).toBe(EDITORIAL_FIXTURES.article.title);

  await page.goto(`/subjects/${subjectId}`);
  await expect(
    page.getByRole("heading", { name: EDITORIAL_FIXTURES.article.title }),
  ).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "Contenu du sujet" }),
  ).toBeVisible();
});
