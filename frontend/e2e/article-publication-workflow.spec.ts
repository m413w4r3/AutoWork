import { expect, test } from "@playwright/test";

test("Article : sélection, production, revue et publication DOCX", async ({
  page,
}) => {
  const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const subjectId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const batchId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const runId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const artifactId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
  const manifestId = "ffffffff-ffff-4fff-8fff-ffffffffffff";
  const hash = "a".repeat(64);
  let productionStarted = false;
  let productionTerminal = false;
  let batchReads = 0;
  let publicationAccepted = false;
  let releasePublished = false;
  let releaseReads = 0;
  let editionReads = 0;
  const seenPaths: string[] = [];

  const edition = () => ({
    id: editionId,
    country: "Iran",
    country_code: "IR",
    period_start: "2026-08-01",
    period_end: "2026-08-31",
    tlp: "AMBER",
    languages: ["fr", "en", "fa"],
    target_articles: 1,
    previous_edition_id: null,
    source_profile: "iran-default",
    status: releasePublished
      ? "published"
      : publicationAccepted
        ? "assembling"
        : productionTerminal && editionReads >= 4
          ? "review"
          : productionStarted
            ? "production"
            : "selection",
    version: publicationAccepted ? 5 : productionStarted ? 3 : 2,
    progress_percent: releasePublished ? 100 : publicationAccepted ? 90 : 40,
    allowed_transitions: releasePublished ? ["archived"] : ["archived"],
    created_at: "2026-08-29T00:00:00Z",
    updated_at: "2026-08-29T00:00:00Z",
  });

  const board = {
    groups: [
      {
        id: "11111111-1111-4111-8111-111111111111",
        edition_id: editionId,
        title: "Campagne Iranian Proxy",
        outcome: "new_subject",
        status: "selected",
        subject_id: subjectId,
        candidates: [],
        score: {
          impact: 3,
          novelty: 3,
          technical_depth: 4,
          hunting_potential: 3,
          actionability: 3,
          source_quality: 3,
          total: 19,
          justifications: {},
        },
        source_relationship_status: "verified",
        needs_source_verification: false,
        needs_source_expansion: false,
        grouping_confidence: "high",
        grouping_justification: "Sélection canonique.",
        historical_comparison: null,
        version: 2,
      },
    ],
    selected_articles: 1,
    ignored: 0,
    undecided: 0,
    target_articles: 1,
    automatic_selection: false,
  };

  const batch = {
    batch_id: batchId,
    edition_id: editionId,
    status: "completed",
    phase: "review",
    next_dispatch_at: null,
    items: 1,
    completed: 1,
    needs_review: 0,
    failed: 0,
    cancelled: 0,
    item_details: [
      {
        position: 1,
        subject_id: subjectId,
        title: "Campagne Iranian Proxy",
        run_id: runId,
        status: "ready",
        current_stage: "assembly",
        pipeline_generation: 1,
        auto_recovery_count: 0,
        error_code: null,
        error_message: null,
      },
    ],
    created_at: "2026-08-29T00:01:00Z",
    started_at: "2026-08-29T00:02:00Z",
    finished_at: "2026-08-29T00:10:00Z",
  };
  const runningBatch = {
    ...batch,
    status: "running",
    phase: "initial",
    completed: 0,
    item_details: batch.item_details.map((item) => ({
      ...item,
      status: "running",
      current_stage: "sources",
    })),
    finished_at: null,
  };

  const review = {
    edition_id: editionId,
    items: [
      {
        position: 1,
        subject_id: subjectId,
        title: "Campagne Iranian Proxy",
        run_id: runId,
        pipeline_generation: 1,
        run_status: "ready",
        document_artifact_id: artifactId,
        document_artifact_version: 1,
        document_input_hash: hash,
        effective_decision_id: null,
        effective_decision: null,
        included: true,
        blocking: false,
        can_retry: false,
        retry_stage: null,
        error_code: null,
        error_message: null,
      },
    ],
    can_accept: true,
  };

  const release = () => ({
    edition_id: editionId,
    edition_status: releasePublished ? "published" : "assembling",
    manifest_id: manifestId,
    manifest_sha256: hash,
    release_id: releasePublished ? "release-1" : null,
    json_available: releasePublished,
    markdown_available: releasePublished,
    docx_available: releasePublished,
    published_at: releasePublished ? "2026-08-29T00:20:00Z" : null,
    assembly_job_id: "assembly-job-1",
    assembly_status: releasePublished ? "succeeded" : "queued",
    assembly_error_code: null,
    assembly_error_message: null,
    can_retry_assembly: false,
  });

  await page.route("/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    seenPaths.push(`${request.method()} ${path}`);

    if (path === `/api/editions/${editionId}`) {
      editionReads += 1;
      await route.fulfill({ json: edition() });
      return;
    }
    if (path === `/api/editions/${editionId}/editorial-groups`) {
      await route.fulfill({ json: board });
      return;
    }
    if (
      path === `/api/editions/${editionId}/production` &&
      request.method() === "POST"
    ) {
      productionStarted = true;
      await route.fulfill({ status: 202, json: runningBatch });
      return;
    }
    if (path === `/api/editions/${editionId}/production`) {
      batchReads += 1;
      if (batchReads > 1) productionTerminal = true;
      await route.fulfill({ json: batchReads === 1 ? runningBatch : batch });
      return;
    }
    if (path === `/api/editions/${editionId}/review`) {
      await route.fulfill({ json: review });
      return;
    }
    if (path === `/api/editions/${editionId}/publication/accept`) {
      publicationAccepted = true;
      await route.fulfill({
        status: 202,
        json: {
          edition_id: editionId,
          edition_status: "assembling",
          manifest_id: manifestId,
          manifest_sha256: hash,
          edition_version: 4,
          batch_id: batchId,
          job_id: "assembly-job-1",
          job_dispatched: true,
        },
      });
      return;
    }
    if (path === `/api/editions/${editionId}/release`) {
      releaseReads += 1;
      if (publicationAccepted && releaseReads > 1) releasePublished = true;
      await route.fulfill({ json: release() });
      return;
    }
    if (path === `/api/subjects/${subjectId}/content`) {
      await route.fulfill({
        json: {
          subject_id: subjectId,
          run_id: runId,
          pipeline_generation: 1,
          artifact_id: artifactId,
          artifact_version: 1,
          artifact_input_hash: hash,
          status: "verified",
          schema_version: "2",
          canonical_content: {
            schema_version: "2",
            title: "Article canonique Iranian Proxy",
            timeline: [],
            synthesis: [
              [{ kind: "text", text: "Contenu vérifié.", source_ids: [] }],
            ],
            indicators: [],
            sources: [],
            uncertainties: [],
          },
          rendered_content: null,
        },
      });
      return;
    }
    await route.fulfill({ status: 404, json: {} });
  });

  await page.goto(`/editions/${editionId}`);
  await expect(page.getByText("0 sélectionné pour ce lot")).toBeVisible();
  await page
    .getByRole("checkbox", { name: "Campagne Iranian Proxy" })
    .check();
  await expect(
    page.getByRole("button", { name: "Lancer la production de 1 article" }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Lancer la production de 1 article" })
    .click();

  await expect(
    page.getByRole("heading", { name: "1 / 1 articles traités" }),
  ).toBeVisible();
  await expect(page.getByText("1 prêts")).toBeVisible();
  await page.reload();

  await expect(
    page.getByRole("heading", { name: "Revue de publication" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Ouvrir" }).click();
  await expect(
    page.getByRole("heading", { name: "Article canonique Iranian Proxy" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Article" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await page.goBack();

  await expect(
    page.getByRole("button", { name: "Accepter la production" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Accepter la production" }).click();
  await expect(
    page.getByRole("heading", { name: "Manifest figé" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Bulletin publié" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Télécharger le bulletin DOCX" }),
  ).toHaveAttribute("href", `/api/editions/${editionId}/release/docx`);

  expect(seenPaths).toEqual(
    expect.arrayContaining([
      `POST /api/editions/${editionId}/production`,
      `GET /api/editions/${editionId}/production`,
      `GET /api/editions/${editionId}/review`,
      `POST /api/editions/${editionId}/publication/accept`,
      `GET /api/editions/${editionId}/release`,
      `GET /api/subjects/${subjectId}/content`,
    ]),
  );
});
