import { expect, test } from "@playwright/test";

test("Édition : production séquentielle de deux articles, revue et DOCX", async ({
  page,
}) => {
  const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const subjectA = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const subjectB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbc";
  const batchId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const runA = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const runB = "dddddddd-dddd-4ddd-8ddd-ddddddddddde";
  const artifactA = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
  const artifactB = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeef";
  const manifestId = "ffffffff-ffff-4fff-8fff-ffffffffffff";
  const hash = "a".repeat(64);
  let productionStarted = false;
  let batchFinished = false;
  let batchReads = 0;
  let allowReview = false;
  let publicationAccepted = false;
  let releasePublished = false;
  let releaseReads = 0;
  const openedSubjects: string[] = [];
  const seenPaths: string[] = [];
  let productionPostBody: unknown = null;

  const edition = () => ({
    id: editionId,
    country: "Iran",
    country_code: "IR",
    period_start: "2026-08-01",
    period_end: "2026-08-31",
    tlp: "AMBER",
    languages: ["fr", "en", "fa"],
    target_articles: 2,
    previous_edition_id: null,
    source_profile: "iran-default",
    status: releasePublished
      ? "published"
      : publicationAccepted
        ? "assembling"
        : batchFinished && allowReview
          ? "review"
          : productionStarted
            ? "production"
            : "selection",
    version: publicationAccepted ? 5 : productionStarted ? 3 : 2,
    progress_percent: releasePublished ? 100 : publicationAccepted ? 90 : 40,
    allowed_transitions: ["archived"],
    created_at: "2026-08-29T00:00:00Z",
    updated_at: "2026-08-29T00:00:00Z",
  });

  const board = {
    groups: [
      {
        id: "11111111-1111-4111-8111-111111111111",
        edition_id: editionId,
        title: "Article A",
        outcome: "new_subject",
        status: "selected",
        subject_id: subjectA,
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
      {
        id: "11111111-1111-4111-8111-111111111112",
        edition_id: editionId,
        title: "Article B",
        outcome: "new_subject",
        status: "selected",
        subject_id: subjectB,
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
    selected_articles: 2,
    ignored: 0,
    undecided: 0,
    target_articles: 2,
    automatic_selection: false,
  };

  const batchState = (
    firstStatus: "running" | "ready",
    secondStatus: "queued" | "running" | "ready",
    completed: number,
    status: "running" | "completed",
  ) => ({
    batch_id: batchId,
    edition_id: editionId,
    status,
    phase: status === "completed" ? "review" : "initial",
    next_dispatch_at: null,
    items: 2,
    completed,
    needs_review: 0,
    failed: 0,
    cancelled: 0,
    item_details: [
      {
        position: 1,
        subject_id: subjectA,
        title: "Article A",
        run_id: runA,
        status: firstStatus,
        current_stage: firstStatus === "running" ? "references" : "assembly",
        pipeline_generation: 2,
        auto_recovery_count: 0,
        error_code: null,
        error_message: null,
      },
      {
        position: 2,
        subject_id: subjectB,
        title: "Article B",
        run_id: runB,
        status: secondStatus,
        current_stage: secondStatus === "running" ? "sources" : "sources",
        pipeline_generation: 2,
        auto_recovery_count: 0,
        error_code: null,
        error_message: null,
      },
    ],
    created_at: "2026-08-29T00:01:00Z",
    started_at: "2026-08-29T00:02:00Z",
    finished_at: status === "completed" ? "2026-08-29T00:10:00Z" : null,
  });

  const review = {
    edition_id: editionId,
    items: [
      {
        position: 1,
        subject_id: subjectA,
        title: "Article A",
        run_id: runA,
        pipeline_generation: 2,
        run_status: "ready",
        document_artifact_id: artifactA,
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
      {
        position: 2,
        subject_id: subjectB,
        title: "Article B",
        run_id: runB,
        pipeline_generation: 2,
        run_status: "ready",
        document_artifact_id: artifactB,
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

  const content = (subjectId: string) => {
    const isA = subjectId === subjectA;
    return {
      subject_id: subjectId,
      run_id: isA ? runA : runB,
      pipeline_generation: 2,
      artifact_id: isA ? artifactA : artifactB,
      artifact_version: 1,
      artifact_input_hash: hash,
      status: "verified",
      schema_version: "2",
      canonical_content: {
        schema_version: "2",
        title: isA ? "Article A" : "Article B",
        timeline: [],
        synthesis: [
          [
            {
              kind: "text",
              text: isA ? "Contenu A." : "Contenu B.",
              source_ids: [],
            },
          ],
        ],
        indicators: [],
        sources: [],
        uncertainties: [],
      },
      rendered_content: null,
    };
  };

  const productionStatus = (subjectId: string) => {
    const isA = subjectId === subjectA;
    const currentRun = isA ? runA : runB;
    const currentArtifact = isA ? artifactA : artifactB;
    const stage = (reused = false) => ({
      status: "succeeded",
      version: 1,
      error_code: null,
      error_message: null,
      reused,
      reused_from_artifact_id: reused ? `${currentArtifact}-source` : null,
      reused_from_created_at: reused ? "2026-08-28T00:00:00Z" : null,
      research_date: "2026-08-28",
    });
    return {
      subject_id: subjectId,
      title: isA ? "Article A" : "Article B",
      status: "ready",
      current_stage: "assembly",
      progress_current: 5,
      progress_total: 5,
      references_conversation_id: null,
      synthesis_conversation_id: null,
      run_id: currentRun,
      pipeline_generation: 2,
      created_at: "2026-08-29T00:02:00Z",
      started_at: "2026-08-29T00:02:00Z",
      finished_at: "2026-08-29T00:10:00Z",
      error_code: null,
      error_message: null,
      error_details: null,
      warnings: [],
      stages: {
        sources: stage(),
        references: stage(true),
        extraction: stage(true),
        synthesis: stage(true),
        assembly: stage(),
      },
    };
  };

  const manifestEntries = [
    {
      position: 1,
      subject_id: subjectA,
      production_run_id: runA,
      document_artifact_id: artifactA,
    },
    {
      position: 2,
      subject_id: subjectB,
      production_run_id: runB,
      document_artifact_id: artifactB,
    },
  ];

  await page.route("/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    seenPaths.push(`${request.method()} ${path}`);

    if (path === `/api/editions/${editionId}`) {
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
      productionPostBody = request.postDataJSON();
      await route.fulfill({
        status: 202,
        json: batchState("running", "queued", 0, "running"),
      });
      return;
    }
    if (path === `/api/editions/${editionId}/production`) {
      batchReads += 1;
      if (batchReads === 1) {
        await route.fulfill({
          json: batchState("running", "queued", 0, "running"),
        });
      } else if (batchReads === 2) {
        await route.fulfill({
          json: batchState("ready", "running", 1, "running"),
        });
      } else {
        batchFinished = true;
        await route.fulfill({
          json: batchState("ready", "ready", 2, "completed"),
        });
      }
      return;
    }
    if (path === `/api/editions/${editionId}/review`) {
      await route.fulfill({ json: review });
      return;
    }
    if (
      path === `/api/editions/${editionId}/publication/accept` &&
      request.method() === "POST"
    ) {
      publicationAccepted = true;
      expect(manifestEntries).toHaveLength(2);
      expect(manifestEntries.map((entry) => entry.position)).toEqual([1, 2]);
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
          manifest_entries: manifestEntries,
        },
      });
      return;
    }
    if (path === `/api/editions/${editionId}/release`) {
      releaseReads += 1;
      if (publicationAccepted && releaseReads > 1) releasePublished = true;
      await route.fulfill({
        json: {
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
        },
      });
      return;
    }
    const subjectMatch = path.match(/^\/api\/subjects\/([^/]+)\/content$/);
    if (subjectMatch) {
      openedSubjects.push(subjectMatch[1]);
      await route.fulfill({ json: content(subjectMatch[1]) });
      return;
    }
    const productionMatch = path.match(
      /^\/api\/subjects\/([^/]+)\/production$/,
    );
    if (productionMatch) {
      await route.fulfill({ json: productionStatus(productionMatch[1]) });
      return;
    }
    await route.fulfill({ status: 404, json: {} });
  });

  await page.goto(`/editions/${editionId}`);
  await expect(
    page.getByRole("heading", { name: "2 articles éligibles" }),
  ).toBeVisible();
  await expect(page.getByText("0 sélectionné pour ce lot")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Sélectionnez au moins un article" }),
  ).toBeDisabled();

  // The real operator gesture: check A and B explicitly. Nothing is
  // pre-armed by loading or reloading the page.
  await page.getByRole("checkbox", { name: "Article A" }).check();
  await page.getByRole("checkbox", { name: "Article B" }).check();
  await expect(page.getByText("2 sélectionnés pour ce lot")).toBeVisible();

  await expect(
    page.getByRole("button", { name: "Lancer la production de 2 articles" }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Lancer la production de 2 articles" })
    .click();

  await expect
    .poll(() => productionPostBody)
    .toEqual({
      subject_ids: [subjectA, subjectB],
    });

  await expect(
    page.getByRole("heading", { name: "0 / 2 articles traités" }),
  ).toBeVisible();
  await expect(page.getByText("Article A")).toBeVisible();
  await expect(page.getByText("Article B")).toBeVisible();
  await expect(page.getByText("En cours").first()).toBeVisible();
  await expect(page.getByText("En attente")).toBeVisible();

  await expect(
    page.getByRole("heading", { name: "1 / 2 articles traités" }),
  ).toBeVisible();
  await expect(page.getByText("Prêt", { exact: true })).toBeVisible();
  await expect(page.getByText("En cours").first()).toBeVisible();

  await expect(
    page.getByRole("heading", { name: "2 / 2 articles traités" }),
  ).toBeVisible();
  allowReview = true;
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Revue de publication" }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Ouvrir" })).toHaveCount(2);

  await page.getByRole("link", { name: "Ouvrir" }).nth(0).click();
  await expect(page.getByText("Article A", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Pipeline" }).click();
  await page.getByText("Diagnostic", { exact: true }).click();
  await expect(
    page.getByText("Références : réutilisée depuis un calcul précédent"),
  ).toBeVisible();
  await page.goBack();

  await page.getByRole("link", { name: "Ouvrir" }).nth(1).click();
  await expect(page.getByText("Article B", { exact: true })).toBeVisible();
  await page.goBack();
  await expect(
    page.getByRole("heading", { name: "Revue de publication" }),
  ).toBeVisible();
  expect(openedSubjects).toEqual([subjectA, subjectB]);

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
      `GET /api/subjects/${subjectA}/content`,
      `GET /api/subjects/${subjectB}/content`,
      `GET /api/subjects/${subjectA}/production`,
    ]),
  );
});
