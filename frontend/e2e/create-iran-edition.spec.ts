import { expect, test } from "@playwright/test";

test("crée une édition Iran depuis le formulaire métier", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const edition = {
    id: "ab440576-6d7d-40da-8777-11067df68b06",
    country: "Iran",
    country_code: "IR",
    period_start: "2026-07-01",
    period_end: "2026-07-31",
    tlp: "AMBER",
    languages: ["fr", "en", "fa"],
    target_major_articles: 2,
    target_briefs: 6,
    previous_edition_id: null,
    source_profile: "iran-default",
    status: "draft",
    version: 1,
    progress_percent: 0,
    allowed_transitions: ["discovery", "archived"],
    created_at: "2026-08-08T00:00:00Z",
    updated_at: "2026-08-08T00:00:00Z",
  };
  let submitted: Record<string, unknown> | null = null;
  let deletedUrl: string | null = null;

  await page.route(
    (url) => url.pathname.startsWith("/api/editions"),
    async (route) => {
      const request = route.request();
      if (
        request.method() === "POST" &&
        request.url().endsWith("/api/editions")
      ) {
        submitted = request.postDataJSON() as Record<string, unknown>;
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify(edition),
        });
        return;
      }
      if (
        request.method() === "DELETE" &&
        new URL(request.url()).pathname.endsWith(edition.id)
      ) {
        deletedUrl = request.url();
        await route.fulfill({ status: 204, body: "" });
        return;
      }
      if (request.url().endsWith(edition.id)) {
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify(edition),
        });
        return;
      }
      if (new URL(request.url()).pathname.includes("/editorial-groups")) {
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            groups: [],
            selected_briefs: 0,
            selected_major: 0,
            target_briefs: 6,
            target_major: 2,
            automatic_selection: false,
          }),
        });
        return;
      }
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }),
      });
    },
  );

  await page.goto("/editions/new");
  expect(pageErrors).toEqual([]);
  await page.getByLabel("Pays", { exact: true }).fill("Iran");
  await page.getByLabel("Code pays").fill("IR");
  await page.getByLabel("Période").fill("2026-07");
  await page.getByLabel("Langues").fill("fr,en,fa");
  await page.getByLabel("Profil de sources").fill("iran-default");
  await page.getByRole("button", { name: "Créer l’édition" }).click();

  await expect(page).toHaveURL(`/editions/${edition.id}`);
  await expect(page.getByRole("heading", { name: "Iran" })).toBeVisible();
  await expect(page.getByText("TLP:AMBER")).toBeVisible();
  expect(submitted).toMatchObject({
    country: "Iran",
    country_code: "IR",
    period_start: "2026-07-01",
    period_end: "2026-07-31",
    languages: ["fr", "en", "fa"],
    previous_edition_id: null,
  });

  await page
    .getByRole("button", { name: "Supprimer définitivement l’édition" })
    .click();
  const confirmation = page.getByLabel(
    "Pour confirmer, saisissez le nom du pays : Iran",
  );
  await confirmation.fill("IRAN");
  await expect(
    page.getByRole("button", { name: "Effacer toutes les données" }),
  ).toBeDisabled();
  await confirmation.fill("Iran");
  await page
    .getByRole("button", { name: "Effacer toutes les données" })
    .click();

  await expect(page).toHaveURL("/editions");
  expect(deletedUrl).toContain(`/api/editions/${edition.id}?version=1`);
});
