import { expect, test } from "@playwright/test";

test("cinq cartes deviennent deux sujets prêts dans un lot atomique et restent responsives", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const edition = {
    id: editionId,
    country: "Iran",
    country_code: "IR",
    period_start: "2026-08-01",
    period_end: "2026-08-31",
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
  const titles = [
    "Cyber Isnaad Front",
    "Nimbus Manticore / UNC1549",
    "MuddyWater reconnaissance/OWA",
    "Seedworm / MuddyWater",
    "Activité visant des PLC Rockwell",
  ];
  const decisions = new Map<string, "brief" | "major" | "ignore">();
  const postedBodies: unknown[] = [];
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
  const groups = () =>
    titles.map((title, index) => {
      const id = `${index + 1}1111111-1111-4111-8111-111111111111`;
      const decision = decisions.get(id);
      return {
        id,
        edition_id: editionId,
        title,
        outcome: "new_subject",
        status:
          decision === "ignore"
            ? "rejected"
            : decision
              ? "selected"
              : "proposed",
        editorial_type:
          decision === "brief" || decision === "major" ? decision : null,
        subject_id:
          decision === "brief" || decision === "major"
            ? `${index + 1}eeeeeee-eeee-4eee-8eee-eeeeeeeeeeee`
            : null,
        presentation: `Présentation neutre de ${title}.`,
        actor_or_campaign: title,
        technical_potential: index === 4 ? 3 : 4,
        technical_potential_reason: "Artefacts techniques annoncés.",
        artifacts: ["ioc", "configurations"],
        publications: [
          {
            title: `Rapport ${title}`,
            url: `https://vendor.example/report-${index + 1}`,
            publisher: "Vendor Research",
            role: "primary",
            published_at: "2026-08-05",
          },
        ],
        uncertainties: ["Attribution à vérifier"],
        publisher_ioc_count_total: 12,
        provisional_ioc_count: 1,
        provisional_ioc_type_counts: { domain: 1 },
        provisional_iocs: [
          {
            raw_value: `ioc-${index + 1}.example`,
            normalized_value: `ioc-${index + 1}.example`,
            proposed_type: "domain",
            declared_type: "domain",
            warnings: [],
          },
        ],
        metadata_incomplete: false,
        candidates: [
          {
            id: `${index + 1}ccccccc-cccc-4ccc-8ccc-cccccccccccc`,
            batch_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            title: `Rapport ${title}`,
            summary: `Présentation neutre de ${title}.`,
            event_date: "2026-08-05",
            source_urls: [`https://vendor.example/report-${index + 1}`],
          },
        ],
        score,
        source_relationship_status: "provisional",
        needs_source_verification: true,
        needs_source_expansion: true,
        grouping_confidence: "high",
        grouping_justification: `Bloc ChatGPT S${index + 1}`,
        historical_comparison: null,
        version: decision ? 2 : 1,
      };
    });

  await page.route("/api/editions/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith(`/api/editions/${editionId}`))
      return route.fulfill({ json: edition });
    if (path.endsWith("/discovery/candidates"))
      return route.fulfill({
        json: { batches: [], candidates: [], total: 0, warning: "" },
      });
    if (
      path.endsWith("/editorial-groups/decisions") &&
      request.method() === "POST"
    ) {
      const payload = request.postDataJSON() as {
        decisions: Array<{
          group_id: string;
          version: number;
          decision: "brief" | "major" | "ignore";
        }>;
      };
      postedBodies.push(payload);
      for (const item of payload.decisions)
        decisions.set(item.group_id, item.decision);
    }
    if (path.includes("/editorial-groups"))
      return route.fulfill({
        json: {
          groups: groups(),
          selected_briefs: [...decisions.values()].filter(
            (item) => item === "brief",
          ).length,
          selected_major: [...decisions.values()].filter(
            (item) => item === "major",
          ).length,
          ignored: [...decisions.values()].filter((item) => item === "ignore")
            .length,
          undecided: titles.length - decisions.size,
          target_briefs: 1,
          target_major: 1,
          automatic_selection: false,
        },
      });
    return route.fulfill({ status: 404, body: "{}" });
  });

  await page.goto(`/editions/${editionId}`);
  await expect(page.locator(".editorial-group-card")).toHaveCount(5);
  await expect
    .poll(() =>
      page.evaluate<boolean>(
        "document.documentElement.scrollWidth <= window.innerWidth",
      ),
    )
    .toBe(true);

  const cards = page.locator(".editorial-group-card");
  await cards.nth(0).getByRole("radio", { name: "Brève" }).check();
  await cards
    .nth(1)
    .getByRole("radio", { name: "Article approfondi + pivots" })
    .check();
  for (let index = 2; index < 5; index += 1)
    await cards.nth(index).getByRole("radio", { name: "Ignorer" }).check();

  await page
    .getByRole("button", { name: "Confirmer la sélection (5)" })
    .click();

  expect(postedBodies).toHaveLength(1);
  expect((postedBodies[0] as { decisions: unknown[] }).decisions).toHaveLength(
    5,
  );
  await expect(page.locator(".editorial-group-card")).toHaveCount(0);
  await expect(
    page.getByText("2 sujets prêts · 1 brève · 1 article principal"),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Ouvrir le sujet" })).toHaveCount(
    2,
  );
});
