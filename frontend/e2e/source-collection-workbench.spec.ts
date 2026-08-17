import { expect, test } from "@playwright/test";

test("collecte résiliente et workbench Sources simplifié", async ({ page }) => {
  const subjectId = "11111111-1111-4111-8111-111111111111";
  const jobId = "22222222-2222-4222-8222-222222222222";
  let unavailableState = "unavailable";
  const source = (index: number, state: string) => ({
    id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    requested_url: `https://${index}.example/report`,
    state,
    proposed_role: index === 1 ? "primary" : "independent",
    relationship_status: "provisional",
    relationship_evidence: "model_proposal",
    source_document_id: ["archived", "completed"].includes(state)
      ? `10000000-0000-4000-8000-${String(index).padStart(12, "0")}`
      : null,
    attempt_count: 1,
    error_reason:
      state === "unavailable"
        ? "Remote server returned HTTP 404"
        : state === "blocked"
          ? "Domain is blocked by collection policy"
          : null,
    fetch_lease_expires_at: null,
    latest_attempt: {
      id: `20000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
      requested_url: `https://${index}.example/report`,
      final_url: `https://${index}.example/report`,
      redirect_chain: [],
      attempted_at: "2026-08-14T10:00:00Z",
      completed_at: "2026-08-14T10:00:01Z",
      http_status:
        state === "unavailable" ? 404 : state === "blocked" ? null : 200,
      declared_content_type: "text/html",
      detected_content_type: ["archived", "completed"].includes(state)
        ? "text/html"
        : null,
      encoded_size: 100,
      encoded_sha256: "a".repeat(64),
      decoded_size: 100,
      decoded_sha256: "b".repeat(64),
      content_encoding: "identity",
      outcome:
        state === "unavailable"
          ? "unavailable"
          : state === "blocked"
            ? "blocked"
            : "succeeded",
      failure_reason: null,
    },
    title: [
      "Tenable déjà archivée",
      "FBI téléchargeable",
      "Source absente",
      "Source SSRF",
    ][index - 1],
    publisher: ["Tenable", "FBI", "Example", "Interne"][index - 1],
    published_at: "2026-07-28",
    tlp: "AMBER",
    logical_filename: ["archived", "completed"].includes(state)
      ? `2026-07-28_TLP AMBER_Rapport ${index}_Publisher.html`
      : null,
    detected_mime_type: ["archived", "completed"].includes(state)
      ? "text/html"
      : null,
  });

  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname.endsWith("/workbench")) {
        await route.fulfill({
          json: {
            subject_id: subjectId,
            sources: [
              source(1, "completed"),
              source(2, "archived"),
              source(3, unavailableState),
              source(4, "blocked"),
            ],
            claims: [],
            indicators: [],
          },
        });
        return;
      }
      if (url.pathname.endsWith("/collection") && request.method() === "POST") {
        await route.fulfill({
          status: 202,
          json: { job_id: jobId, duplicate: false },
        });
        return;
      }
      if (url.pathname === `/api/jobs/${jobId}`) {
        await route.fulfill({
          json: {
            id: jobId,
            kind: "source.collect",
            aggregate_type: "subject",
            aggregate_id: subjectId,
            status: "succeeded",
            progress_current: 4,
            progress_total: 4,
            user_message:
              "4 publications traitées · 2 archivées · 2 avertissements",
            attempt: 1,
            max_attempts: 1,
            next_retry_at: null,
            started_at: "2026-08-14T10:00:00Z",
            finished_at: "2026-08-14T10:00:04Z",
            heartbeat_at: "2026-08-14T10:00:04Z",
            error_code: null,
            error_message: null,
            error_details: null,
            correlation_id: "correlation-test",
            output_reference: "provenance://events/summary",
            cancellation_requested: false,
            created_at: "2026-08-14T10:00:00Z",
            updated_at: "2026-08-14T10:00:04Z",
          },
        });
        return;
      }
      if (url.pathname.endsWith("/retry") && request.method() === "POST") {
        unavailableState = "archived";
        await route.fulfill({
          status: 202,
          json: { job_id: jobId, duplicate: false },
        });
        return;
      }
      await route.fulfill({ status: 404, json: {} });
    },
  );

  await page.goto(`/subjects/${subjectId}`);
  await expect(page.getByText("4 publications", { exact: true })).toBeVisible();
  await expect(page.getByText("Tenable déjà archivée")).toBeVisible();
  await expect(
    page.getByText("2026-07-28_TLP AMBER_Rapport 1_Publisher.html"),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Télécharger" })).toHaveCount(2);
  await expect(
    page.locator("details.technical-details").first(),
  ).not.toHaveAttribute("open");
  await expect(page.getByRole("button", { name: "Preuves" })).toBeHidden();
  await expect(page.getByRole("button", { name: "IOC" })).toBeHidden();
  await expect(page.getByRole("button", { name: "Extraction" })).toBeHidden();
  await expect(
    page.getByRole("button", { name: "Conversations" }),
  ).toBeHidden();

  await page.getByRole("button", { name: "Collecter les sources" }).click();
  await expect(page.getByText("100 % — 4/4")).toBeVisible();
  await expect(page.getByText(/2 avertissements/)).toBeVisible();
  await expect(page.getByText("Échec")).toHaveCount(0);

  await page.getByRole("button", { name: "Réessayer" }).click();
  await expect(page.getByText("3 archivées")).toBeVisible();
  await expect(page.getByText("Source SSRF")).toBeVisible();
  await expect(page.getByRole("button", { name: "Réessayer" })).toHaveCount(0);
});
