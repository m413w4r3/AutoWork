import { expect, test } from "@playwright/test";

import { EDITORIAL_FIXTURES, stubEditorialApi } from "./fixtures/editorialApi";

test("editorial stubbed app loads with the synthetic fixture", async ({
  page,
}) => {
  const api = await stubEditorialApi(page);
  const subjectId = EDITORIAL_FIXTURES.subject.id;
  const [firstParagraph, secondParagraph] =
    EDITORIAL_FIXTURES.article.paragraphs;

  expect(firstParagraph.id).not.toBe(secondParagraph.id);
  expect(firstParagraph.position).not.toBe(secondParagraph.position);
  expect(firstParagraph.text).toBe(secondParagraph.text);

  await page.goto(`/subjects/${subjectId}`);
  await expect(
    page.getByRole("heading", { name: EDITORIAL_FIXTURES.article.title }),
  ).toBeVisible();

  const subjectTabs = page.getByRole("navigation", {
    name: "Contenu du sujet",
  });
  await expect(subjectTabs).toBeVisible();
  await expect(
    subjectTabs.getByRole("button", { name: "Article" }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByRole("heading", { name: "Aucun contenu" }),
  ).toBeVisible();
  await expect.poll(() => api.requests).toContain(`/api/subjects/${subjectId}`);
  await expect
    .poll(() => api.requests)
    .toContain(`/api/subjects/${subjectId}/content`);
  expect(api.unexpectedRequests).toEqual([]);
});
