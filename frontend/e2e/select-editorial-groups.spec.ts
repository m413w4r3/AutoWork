import { expect, test } from "@playwright/test";

test("sélectionne une brève et un article principal depuis le board", async ({
  page,
}) => {
  const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const edition = {
    id: editionId,
    country: "Iran",
    country_code: "IR",
    period_start: "2026-07-01",
    period_end: "2026-07-31",
    tlp: "AMBER",
    languages: ["fr", "en", "fa"],
    target_major_articles: 1,
    target_briefs: 1,
    previous_edition_id: null,
    source_profile: "iran-default",
    status: "selection",
    version: 3,
    progress_percent: 28,
    allowed_transitions: ["production", "archived"],
    created_at: "2026-08-08T00:00:00Z",
    updated_at: "2026-08-10T00:00:00Z",
  };
  const candidate = (suffix: string, title: string) => ({
    id: `cccccccc-cccc-4ccc-8ccc-ccccccccccc${suffix}`,
    batch_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    title,
    summary: `Résumé ${title}`,
    event_date: "2026-07-10",
    source_urls: [`https://${suffix}.example/report`],
  });
  const score = {
    impact: 3,
    novelty: 3,
    technical_depth: 4,
    hunting_potential: 3,
    actionability: 3,
    source_quality: 2,
    total: 18,
    justifications: {
      impact: "Secteurs ciblés",
      novelty: "Nouveauté à confirmer",
      technical_depth: "Détails techniques",
      hunting_potential: "IOC proposés",
      actionability: "Mesures possibles",
      source_quality: "Sources provisoires",
    },
  };
  const groups = ["A", "B"].map((suffix, index) => ({
    id: `${index + 1}${index + 1}${index + 1}${index + 1}${index + 1}${index + 1}${index + 1}${index + 1}-1111-4111-8111-111111111111`,
    edition_id: editionId,
    title: `Campagne ${suffix}`,
    outcome: index ? "ambiguous_review" : "new_subject",
    status: "proposed",
    editorial_type: null,
    subject_id: null,
    candidates: [candidate(String(index + 1), `Publication ${suffix}`)],
    score,
    source_relationship_status: "provisional",
    needs_source_verification: true,
    needs_source_expansion: true,
    grouping_confidence: index ? "low" : "high",
    grouping_justification: index ? "Rapprochement ambigu" : "Nouveau sujet",
    historical_comparison: null,
    version: 1,
  }));
  const selections: string[] = [];

  await page.route("/api/editions/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith(`/api/editions/${editionId}`)) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(edition),
      });
      return;
    }
    if (path.includes("/discovery/candidates")) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          batches: [],
          candidates: [],
          total: 0,
          warning: "",
        }),
      });
      return;
    }
    if (path.includes("/editorial-groups")) {
      if (request.method() === "POST" && path.endsWith("/select")) {
        const payload = request.postDataJSON() as { editorial_type: string };
        selections.push(payload.editorial_type);
      }
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          groups,
          selected_briefs: selections.filter((item) => item === "brief").length,
          selected_major: selections.filter((item) => item === "major").length,
          target_briefs: 1,
          target_major: 1,
          automatic_selection: false,
        }),
      });
      return;
    }
    await route.fulfill({ status: 404, body: "{}" });
  });

  await page.goto(`/editions/${editionId}`);
  const groupA = page
    .getByRole("heading", { name: "Campagne A" })
    .locator("..");
  await groupA
    .getByRole("button", { name: "Sélectionner comme brève" })
    .click();
  const groupB = page
    .getByRole("heading", { name: "Campagne B" })
    .locator("..");
  await groupB.getByLabel("Format éditorial").selectOption("major");
  await groupB
    .getByRole("button", { name: "Sélectionner comme article principal" })
    .click();

  expect(selections).toEqual(["brief", "major"]);
  await expect(
    page
      .getByText(/Recherche effectuée depuis les citations visibles de ChatGPT/)
      .first(),
  ).toBeVisible();
});
