import { expect, test } from "@playwright/test";

test("une réponse ChatGPT incomplète expose les trois récupérations humaines", async ({
  page,
}) => {
  const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const jobId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const modelRunId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const preview = {
    sha256: "d".repeat(64),
    subject_count: 5,
    publication_count: 14,
    ioc_count: 63,
    ioc_type_counts: { md5: 17, domain: 33, ipv4: 13 },
    warnings: ["Une date reste inconnue"],
    subjects: [
      "Cyber Isnaad Front",
      "Nimbus Manticore / UNC1549",
      "MuddyWater reconnaissance/OWA",
      "Seedworm / MuddyWater",
      "Activité visant des PLC Rockwell",
    ],
  };
  const calls = { visible: 0, manual: 0, complete: 0 };

  await page.route("/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === `/api/editions/${editionId}`) {
      return route.fulfill({
        json: {
          id: editionId,
          country: "Iran",
          country_code: "IR",
          period_start: "2026-08-01",
          period_end: "2026-08-31",
          tlp: "AMBER",
          languages: ["fr", "en", "fa"],
          target_articles: 8,
          previous_edition_id: null,
          source_profile: "iran-default",
          status: "discovery",
          version: 1,
          progress_percent: 10,
          allowed_transitions: ["selection", "archived"],
          created_at: "2026-08-13T10:00:00Z",
          updated_at: "2026-08-13T10:00:00Z",
        },
      });
    }
    if (path.endsWith("/discovery/candidates")) {
      return route.fulfill({
        json: { batches: [], candidates: [], total: 0, warning: "provisoire" },
      });
    }
    if (path.includes("/editorial-groups")) {
      return route.fulfill({
        json: {
          groups: [],
          selected_articles: 0,
          ignored: 0,
          undecided: 0,
          target_articles: 8,
          automatic_selection: false,
        },
      });
    }
    if (path.endsWith("/events")) return route.fulfill({ status: 204 });
    if (path === `/api/jobs/${jobId}`) {
      return route.fulfill({
        json: {
          id: jobId,
          kind: "discover_edition",
          aggregate_type: "edition",
          aggregate_id: editionId,
          status: "waiting_human",
          progress_current: 2,
          progress_total: 4,
          user_message:
            "ChatGPT s'est arrêté sans produire de réponse finale. La conversation a été conservée et peut être reprise.",
          attempt: 1,
          max_attempts: 1,
          next_retry_at: null,
          started_at: "2026-08-13T10:00:00Z",
          finished_at: null,
          heartbeat_at: "2026-08-13T10:05:00Z",
          error_code: null,
          error_message: null,
          error_details: {
            phase: "chatgpt_incomplete",
            reason: "no_final_answer",
            model_run_id: modelRunId,
            bridge_run_id: "resp_bridge",
          },
          correlation_id: "recovery-e2e",
          output_reference: null,
          cancellation_requested: false,
          created_at: "2026-08-13T10:00:00Z",
          updated_at: "2026-08-13T10:05:00Z",
        },
      });
    }
    if (path.endsWith("/visible/preview")) {
      calls.visible += 1;
      return route.fulfill({ json: preview });
    }
    if (path.endsWith("/manual/preview")) {
      calls.manual += 1;
      return route.fulfill({ json: preview });
    }
    if (path.endsWith("/complete")) {
      calls.complete += 1;
      return route.fulfill({
        status: 202,
        json: { job_id: jobId, status: "queued", reused: true },
      });
    }
    return route.fulfill({ status: 404, body: "{}" });
  });

  await page.addInitScript(
    ({ edition, job }) =>
      localStorage.setItem(`cti-discovery-job:${edition}`, job),
    { edition: editionId, job: jobId },
  );
  await page.goto(`/editions/${editionId}`);

  await expect(
    page.getByText(
      "ChatGPT s’est arrêté sans produire de réponse finale. La conversation a été conservée et peut être reprise.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Récupérer la réponse déjà affichée" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Demander à ChatGPT de terminer" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Coller une réponse", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Abandonner la recherche" }),
  ).toBeVisible();

  await page
    .getByRole("button", { name: "Récupérer la réponse déjà affichée" })
    .click();
  await expect(page.getByLabel("Aperçu du rapport")).toContainText(
    "5 sujets · 14 publications · 63 IOC provisoires",
  );
  await expect(page.getByLabel("Aperçu du rapport")).toContainText(
    "Cyber Isnaad Front",
  );
  await page.getByRole("button", { name: "Annuler" }).click();

  await page
    .getByRole("button", { name: "Coller une réponse", exact: true })
    .click();
  await page
    .getByLabel("Rapport Markdown")
    .fill("## SUBJECT S1\ntitle: Import manuel");
  await page.getByRole("button", { name: "Prévisualiser le rapport" }).click();
  await expect(page.getByLabel("Aperçu du rapport")).toContainText("md5: 17");

  await page
    .getByRole("button", { name: "Demander à ChatGPT de terminer" })
    .click();
  await expect
    .poll(() => calls)
    .toEqual({ visible: 1, manual: 1, complete: 1 });
});
