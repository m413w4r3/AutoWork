import { expect, test } from "@playwright/test";

test("Iran : recherche ChatGPT, parsing local, regroupement et sélection d'un article", async ({
  page,
}) => {
  const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  let searched = false;
  let jobCompleted = false;
  let jobPolls = 0;
  let merged = false;
  let selected = false;
  let selectionOpened = false;
  const edition = {
    id: editionId,
    country: "Iran",
    country_code: "IR",
    period_start: "2026-05-01",
    period_end: "2026-05-31",
    tlp: "AMBER",
    languages: ["fr", "en", "fa"],
    target_articles: 3,
    previous_edition_id: null,
    source_profile: "iran-default",
    status: "discovery",
    version: 2,
    progress_percent: 25,
    allowed_transitions: ["selection", "archived"],
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
  };
  const currentEdition = () => ({
    ...edition,
    status: selectionOpened ? "selection" : "discovery",
    allowed_transitions: selectionOpened
      ? ["production", "archived"]
      : ["selection", "archived"],
  });
  const source = (
    id: string,
    title: string,
    url: string,
    publisher: string,
  ) => ({
    id,
    url,
    canonical_url: url,
    raw_url: url,
    local_ref: "P1",
    source_ref: `source-${id.slice(0, 8)}`,
    title,
    publisher,
    role: "primary",
    published_at: "2026-05-20",
    event_date: null,
    citation: null,
    period_relation: "in_period",
    ioc_presence: "declared",
    ioc_declared_count: 20,
    ioc_visible_count: null,
    parsing_warnings: [],
    verification_status: "unverified",
    relationship_status: "provisional",
    verification_changed_at: null,
    verification_changed_by: null,
  });
  const candidate = (
    id: string,
    localRef: string,
    title: string,
    publication: ReturnType<typeof source>,
  ) => ({
    id,
    batch_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    local_ref: localRef,
    title,
    summary: "Présentation neutre issue du rapport ChatGPT.",
    novelty: "Proposition à vérifier.",
    technical_potential: 3,
    technical_potential_reason: "Artefacts techniques annoncés.",
    event_date: null,
    uncertainties: ["Métadonnées non vérifiées"],
    relevance_reasons: ["Publication dans la période"],
    actors: [],
    campaigns: [],
    malware: [],
    cves: [],
    victims: [],
    sectors: [],
    countries: [],
    likely_artifacts: ["ioc", "configurations"],
    iocs: [],
    editorial_status: "proposed",
    sources: [publication],
    incomplete_sources: [],
    actor_or_campaign: "unknown",
    parsing_warnings: [],
    context_only: false,
    selectable: true,
    valid_publication_count: 1,
    incomplete_publication_count: 0,
  });
  const cyfirma = candidate(
    "cccccccc-cccc-4ccc-8ccc-ccccccccccc1",
    "S1",
    "CYFIRMA — APT Quarterly Report: Apr to Jun 2026",
    source(
      "dddddddd-dddd-4ddd-8ddd-ddddddddddd1",
      "APT Quarterly Report: Apr to Jun 2026",
      "https://cyfirma.example/apt-quarterly",
      "CYFIRMA",
    ),
  );
  const ncc = candidate(
    "cccccccc-cccc-4ccc-8ccc-ccccccccccc2",
    "S2",
    "NCC Group — Monthly Threat Pulse – Review of May 2026",
    source(
      "dddddddd-dddd-4ddd-8ddd-ddddddddddd2",
      "Monthly Threat Pulse – Review of May 2026",
      "https://ncc.example/monthly-pulse",
      "NCC Group",
    ),
  );
  const score = {
    impact: 2,
    novelty: 2,
    technical_depth: 3,
    hunting_potential: 2,
    actionability: 2,
    source_quality: 2,
    total: 13,
    justifications: {
      impact: "À vérifier",
      novelty: "À vérifier",
      technical_depth: "Potentiel déclaré",
      hunting_potential: "IOC annoncés",
      actionability: "À vérifier",
      source_quality: "Source provisoire",
    },
  };
  const groups = () => [
    {
      id: "11111111-1111-4111-8111-111111111111",
      edition_id: editionId,
      title: cyfirma.title,
      outcome: "new_subject",
      status: selected ? "selected" : "proposed",
      subject_id: selected ? "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee" : null,
      candidates: merged ? [cyfirma, ncc] : [cyfirma],
      score,
      source_relationship_status: "provisional",
      needs_source_verification: true,
      needs_source_expansion: true,
      grouping_confidence: "high",
      grouping_justification: merged
        ? "Fusion décidée par l'analyste."
        : "Bloc ChatGPT S1",
      historical_comparison: null,
      version: merged || selected ? 2 : 1,
    },
    {
      id: "22222222-2222-4222-8222-222222222222",
      edition_id: editionId,
      title: ncc.title,
      outcome: "new_subject",
      status: merged ? "superseded" : "proposed",
      subject_id: null,
      candidates: [ncc],
      score,
      source_relationship_status: "provisional",
      needs_source_verification: true,
      needs_source_expansion: true,
      grouping_confidence: "high",
      grouping_justification: "Bloc ChatGPT S2",
      historical_comparison: null,
      version: merged ? 2 : 1,
    },
  ];

  await page.route("/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === `/api/editions/${editionId}`)
      return route.fulfill({ json: currentEdition() });
    if (
      path === `/api/editions/${editionId}/transitions` &&
      request.method() === "POST"
    ) {
      selectionOpened = true;
      return route.fulfill({ json: currentEdition() });
    }
    if (path.endsWith("/discovery/candidates"))
      return route.fulfill({
        json: {
          batches:
            searched && jobCompleted
              ? [
                  {
                    id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    complementary_axis: "initial",
                    queries: [],
                    citations: [],
                    discovery_model_run_id:
                      "ffffffff-ffff-4fff-8fff-ffffffffffff",
                    created_at: "2026-06-01T00:00:00Z",
                    source_mode: "model_declared_urls",
                    bridge_capabilities: {},
                    citation_count: 0,
                    source_coverage_complete: false,
                    source_coverage_incomplete_reason: "non exhaustif",
                    report_sha256: "a".repeat(64),
                    parser_version: "chatgpt-markdown-v1",
                    parsing_status: "completed",
                    parsing_warnings: [],
                    archived_report_url: "/report.md",
                  },
                ]
              : [],
          candidates: searched && jobCompleted ? [cyfirma, ncc] : [],
          total: searched && jobCompleted ? 2 : 0,
          merge_stats: {
            raw_batch_count: searched && jobCompleted ? 1 : 0,
            raw_candidate_count: searched && jobCompleted ? 2 : 0,
            consolidated_candidate_count: searched && jobCompleted ? 2 : 0,
            unique_publication_count: searched && jobCompleted ? 2 : 0,
            duplicate_publication_occurrence_count: 0,
          },
          warning: "provisoire",
        },
      });
    if (path.endsWith("/discovery") && request.method() === "POST") {
      searched = true;
      return route.fulfill({
        status: 202,
        json: {
          job_id: "99999999-9999-4999-8999-999999999999",
          status: "running",
          reused: false,
        },
      });
    }
    if (path.endsWith("/events")) return route.fulfill({ status: 204 });
    if (path.startsWith("/api/jobs/")) {
      jobPolls += 1;
      const running = jobPolls === 1;
      jobCompleted = !running;
      return route.fulfill({
        json: {
          id: "99999999-9999-4999-8999-999999999999",
          kind: "discover_edition",
          aggregate_type: "edition",
          aggregate_id: editionId,
          status: running ? "running" : "succeeded",
          progress_current: running ? 2 : 4,
          progress_total: 4,
          user_message: running
            ? "ChatGPT recherche et analyse les sources"
            : "Analyse locale terminée",
          attempt: 1,
          max_attempts: 1,
          next_retry_at: null,
          started_at: "2026-06-01T00:00:00Z",
          finished_at: running ? null : "2026-06-01T00:10:02Z",
          heartbeat_at: running
            ? "2026-06-01T00:10:00Z"
            : "2026-06-01T00:10:02Z",
          error_code: null,
          error_message: null,
          error_details: running
            ? {
                phase: "background_bridge_wait",
                model_run_id: "model-run-e2e",
                bridge_run_id: "bridge-run-e2e",
                last_job_heartbeat: "2026-06-01T00:10:00Z",
                bridge_state: "waiting_background",
                poll_count: 31,
                elapsed_seconds: 600,
                correlation_id: "iran-e2e",
              }
            : null,
          correlation_id: "iran-e2e",
          output_reference: "discovery-batch://test",
          cancellation_requested: false,
          created_at: "2026-06-01T00:00:00Z",
          updated_at: running ? "2026-06-01T00:10:00Z" : "2026-06-01T00:10:02Z",
        },
      });
    }
    if (path.includes("/editorial-groups")) {
      if (request.method() === "POST" && path.endsWith("/merge")) merged = true;
      if (request.method() === "POST" && path.endsWith("/decisions"))
        selected = true;
      return route.fulfill({
        json: {
          groups: groups(),
          selected_articles: selected ? 1 : 0,
          ignored: 0,
          undecided: selected ? 0 : merged ? 1 : 2,
          target_articles: 3,
          automatic_selection: false,
        },
      });
    }
    return route.fulfill({ status: 404, body: "{}" });
  });

  await page.clock.install({ time: new Date("2026-06-01T00:10:00Z") });
  await page.goto(`/editions/${editionId}`);
  await page
    .getByRole("button", { name: "Nouvelle recherche ChatGPT" })
    .dispatchEvent("click");
  await expect(
    page.getByRole("heading", {
      name: "ChatGPT recherche et analyse les sources",
    }),
  ).toBeVisible();
  await expect(page.getByText(/Temps écoulé : \d+ s/)).toBeVisible();
  await expect(page.getByText("bridge-run-e2e")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: cyfirma.title }).first(),
  ).not.toBeVisible();

  await page.clock.fastForward(2_100);
  await page.getByText("Détails techniques de la découverte").click();
  await expect(
    page.getByRole("heading", { name: cyfirma.title }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: ncc.title }).first(),
  ).toBeVisible();

  await page.getByRole("button", { name: "Ouvrir la sélection" }).click();
  await expect(
    page.getByRole("heading", { name: "Sélection des sujets" }),
  ).toBeVisible();
  await page.getByText("Organiser les publications").click();
  const firstGroup = page
    .locator(".advanced-group-card")
    .filter({ hasText: cyfirma.title });
  const secondGroup = page
    .locator(".advanced-group-card")
    .filter({ hasText: ncc.title });
  await firstGroup.getByLabel("Retenir pour une fusion").check();
  await secondGroup.getByLabel("Retenir pour une fusion").check();
  await page
    .getByRole("button", { name: "Fusionner les groupes cochés" })
    .click();
  await expect(page.locator(".editorial-group-card")).toHaveCount(1);
  await page
    .locator(".editorial-group-card")
    .getByRole("radio", { name: "Article" })
    .check();
  await page
    .getByRole("button", { name: "Confirmer la sélection (1)" })
    .click();
  await expect(page.getByText("1 article prêt")).toBeVisible();
});
