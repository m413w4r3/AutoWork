import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

const subjectId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const artifactId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const canonicalContent = {
  schema_version: "4",
  subject_id: subjectId,
  publication_language: "fr",
  title: "Publication historique synthétique",
  lead: [],
  sections: [],
  timeline: [],
  indicators: [],
  sources: [],
  uncertainties: [],
  tables: [],
  diagrams: [],
  figures: [],
};

test("opens a V4 publication as desktop read-only history", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.route(
    `**/api/subjects/${subjectId}/production/artifacts/publication/${artifactId}`,
    async (route) => {
      await route.fulfill({
        json: {
          artifact_id: artifactId,
          production_run_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
          subject_id: subjectId,
          stage: "publication",
          version: 4,
          input_hash: "a".repeat(64),
          canonical_sha256: "b".repeat(64),
          status: "verified",
          metadata: {},
          publication_kind: "article",
          read_only_reason: "historical_version",
          canonical_content: canonicalContent,
        },
      });
    },
  );

  await page.goto(
    `/subjects/${subjectId}/editor?publication_artifact_id=${artifactId}`,
  );

  await expect(
    page.getByText("Version historique — lecture seule"),
  ).toBeVisible();
  await expect(page.getByText("Raison : version historique.")).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Publication historique synthétique",
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Revenir à la version courante" }),
  ).toHaveAttribute("href", `/subjects/${subjectId}/editor`);
  await expect(page.getByRole("textbox")).toHaveCount(0);

  const screenshotPath = resolve(
    process.cwd(),
    "../.autowork-runs/shots-t-m7-27/legacy-read-only-desktop.png",
  );
  mkdirSync(resolve(screenshotPath, ".."), { recursive: true });
  await page.screenshot({ path: screenshotPath });

  await page
    .getByRole("link", { name: "Revenir à la version courante" })
    .click();
  await page.waitForURL(
    (url) =>
      url.pathname === `/subjects/${subjectId}/editor` &&
      !url.searchParams.has("publication_artifact_id"),
  );
});
