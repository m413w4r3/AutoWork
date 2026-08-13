import { expect, test } from "@playwright/test";

test("parcourt candidat Iran, brève fondée sur preuves, puis validation", async ({
  page,
}) => {
  const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const subjectId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  let selected = false;
  let packFrozen = false;
  let briefState: "empty" | "draft" | "approved" = "empty";
  const edition = {
    id: editionId,
    country: "Iran",
    country_code: "IR",
    period_start: "2026-07-01",
    period_end: "2026-07-31",
    tlp: "AMBER",
    languages: ["fr", "fa"],
    target_major_articles: 1,
    target_briefs: 1,
    previous_edition_id: null,
    source_profile: "iran-default",
    status: "selection",
    version: 2,
    progress_percent: 30,
    allowed_transitions: ["production", "archived"],
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-10T00:00:00Z",
  };
  const group = () => ({
    id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    edition_id: editionId,
    title: "Campagne ciblant des administrations iraniennes",
    outcome: "new_subject",
    status: selected ? "selected" : "proposed",
    editorial_type: selected ? "brief" : null,
    subject_id: selected ? subjectId : null,
    candidates: [
      {
        id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        batch_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        title: "Rapport technique Iran",
        summary: "Une campagne documentée par une source archivée.",
        event_date: "2026-07-12",
        source_urls: ["https://research.example/iran"],
      },
    ],
    score: {
      impact: 3,
      novelty: 3,
      technical_depth: 4,
      hunting_potential: 2,
      actionability: 3,
      source_quality: 3,
      total: 18,
      justifications: {
        impact: "Administrations ciblées",
        novelty: "Nouveau rapport",
        technical_depth: "Détails techniques",
        hunting_potential: "IOC validés",
        actionability: "Mesures possibles",
        source_quality: "Source archivée",
      },
    },
    source_relationship_status: "verified",
    needs_source_verification: false,
    needs_source_expansion: false,
    grouping_confidence: "high",
    grouping_justification: "Métadonnées concordantes",
    historical_comparison: null,
    version: selected ? 2 : 1,
  });
  const brief = () => ({
    subject_id: subjectId,
    pack: packFrozen
      ? {
          id: "11111111-1111-4111-8111-111111111111",
          version: 1,
          content_hash: "a".repeat(64),
          object_hashes: ["b".repeat(64)],
          source_count: 1,
          claim_count: 1,
          indicator_count: 0,
          entity_count: 1,
          uncertainty_count: 1,
          created_by: "dev-analyst",
        }
      : null,
    draft:
      briefState === "empty"
        ? null
        : {
            id: "22222222-2222-4222-8222-222222222222",
            version: 1,
            pack_id: "11111111-1111-4111-8111-111111111111",
            pack_hash: "a".repeat(64),
            title: "Iran : une campagne cible des administrations",
            provider: "qwen",
            stale: false,
          },
    blocks:
      briefState === "empty"
        ? []
        : [
            {
              id: "33333333-3333-4333-8333-333333333333",
              sentences: [
                {
                  id: "44444444-4444-4444-8444-444444444444",
                  text: "Une campagne a ciblé des administrations iraniennes.",
                  factual: true,
                  claim_ids: ["55555555-5555-4555-8555-555555555555"],
                  indicator_ids: [],
                  evidence: [
                    {
                      id: "55555555-5555-4555-8555-555555555555",
                      kind: "fact",
                      value: "ciblé des administrations iraniennes",
                      source_id: "66666666-6666-4666-8666-666666666666",
                      source_span: { start: 10, end: 48 },
                    },
                  ],
                },
              ],
            },
          ],
    limits: [],
    references: [],
    versions: [],
    status: briefState,
    qa:
      briefState === "empty"
        ? {}
        : {
            factual_sentences_covered: true,
            claim_references_in_pack: true,
            source_references_present: true,
            validated_indicators_only: true,
            current_evidence_pack: true,
          },
    qa_errors: [],
    diff: "",
  });

  await page.route("/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === `/api/editions/${editionId}`)
      return route.fulfill({ json: edition });
    if (path.endsWith("/discovery/candidates"))
      return route.fulfill({
        json: { batches: [], candidates: [], total: 0, warning: "" },
      });
    if (path.includes("/editorial-groups")) {
      if (request.method() === "POST" && path.endsWith("/decisions"))
        selected = true;
      return route.fulfill({
        json: {
          groups: [group()],
          selected_briefs: selected ? 1 : 0,
          selected_major: 0,
          ignored: 0,
          undecided: selected ? 0 : 1,
          target_briefs: 1,
          target_major: 1,
          automatic_selection: false,
        },
      });
    }
    if (path.endsWith(`/subjects/${subjectId}/workbench`))
      return route.fulfill({
        json: {
          subject_id: subjectId,
          sources: [],
          claims: [],
          indicators: [],
        },
      });
    if (path.startsWith("/api/jobs/")) {
      if (path.endsWith("/events")) return route.fulfill({ status: 204 });
      return route.fulfill({
        json: {
          id: "99999999-9999-4999-8999-999999999999",
          kind: "brief.generate",
          aggregate_type: "subject",
          aggregate_id: subjectId,
          status: "succeeded",
          progress_current: 1,
          progress_total: 1,
          user_message: "Brève générée",
          attempt: 1,
          max_attempts: 2,
          next_retry_at: null,
          started_at: "2026-08-10T12:00:00Z",
          finished_at: "2026-08-10T12:00:01Z",
          heartbeat_at: "2026-08-10T12:00:01Z",
          error_code: null,
          error_message: null,
          correlation_id: "e2e",
          output_reference: "brief-draft://test",
          cancellation_requested: false,
          created_at: "2026-08-10T12:00:00Z",
          updated_at: "2026-08-10T12:00:01Z",
        },
      });
    }
    if (path.includes(`/subjects/${subjectId}/brief`)) {
      if (request.method() === "POST" && path.endsWith("/freeze"))
        packFrozen = true;
      if (request.method() === "POST" && path.endsWith("/generate")) {
        packFrozen = true;
        briefState = "draft";
        return route.fulfill({
          status: 202,
          json: {
            job_id: "99999999-9999-4999-8999-999999999999",
            duplicate: false,
          },
        });
      }
      if (request.method() === "POST" && path.endsWith("/approve"))
        briefState = "approved";
      return route.fulfill({ json: brief() });
    }
    return route.fulfill({ status: 404, body: "{}" });
  });

  await page.goto(`/editions/${editionId}`);
  await page.getByRole("radio", { name: "Brève" }).check();
  await page
    .getByRole("button", { name: "Confirmer la sélection (1)" })
    .click();
  await page.getByRole("link", { name: "Ouvrir le sujet" }).click();
  await page.getByRole("button", { name: "Brève" }).click();
  await page.getByRole("button", { name: "Geler les preuves" }).click();
  await page.getByRole("button", { name: "Générer la brève" }).click();

  await expect(
    page
      .getByLabel("Preuves de la phrase 1")
      .getByText("ciblé des administrations iraniennes"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Approuver" }).click();
  await expect(page.getByText("approved", { exact: true })).toBeVisible();
});
