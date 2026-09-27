import { expect, test } from "@playwright/test";

import {
  selectionWireBoard,
  selectionWireItem,
  selectionWireLastDecision,
} from "./support/selectionWire";

test("Sujet : sélection, production, revue et publication DOCX", async ({
  page,
}) => {
  const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
  const subjectId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
  const batchId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const runId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
  const artifactId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
  const manifestId = "ffffffff-ffff-4fff-8fff-ffffffffffff";
  const hash = "a".repeat(64);
  const discoverySubjectId = "9a9a9a9a-9a9a-4a9a-8a9a-9a9a9a9a9a9a";
  let batchReads = 0;
  let batchStarted = false;
  let selectionConfirmed = false;
  let publicationAccepted = false;
  let releasePublished = false;
  let releaseReads = 0;
  let productionSurfaceVisited = false;
  let productionPostBody: unknown = null;
  const seenPaths: string[] = [];

  const edition = () => ({
    id: editionId,
    country: "Iran",
    country_code: "IR",
    period_start: "2026-08-01",
    period_end: "2026-08-31",
    tlp: "AMBER",
    languages: ["fr", "en", "fa"],
    state: "open",
    version: 2,
    created_at: "2026-08-29T00:00:00Z",
    updated_at: "2026-08-29T00:00:00Z",
  });

  const selectionBoard = () =>
    selectionWireBoard({
      edition_id: editionId,
      snapshot_id: "88888888-8888-4888-8888-888888888888",
      items: [
        selectionWireItem({
          discovery_subject_id: discoverySubjectId,
          title: "Campagne Iranian Proxy",
          summary: "Présentation neutre de la campagne.",
          actor_or_campaign: "Iranian Proxy",
          effective_state: selectionConfirmed ? "selected" : "undecided",
          subject_id: selectionConfirmed ? subjectId : null,
          last_decision: selectionConfirmed
            ? selectionWireLastDecision({
                id: "decision-1",
                subject_id: subjectId,
              })
            : null,
        }),
      ],
    });

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

  const productionBoard = (
    activeBatch: typeof runningBatch | null,
    recentBatches: readonly (typeof batch)[],
  ) => {
    const active = activeBatch !== null;
    const completed = recentBatches.length > 0;
    return {
      edition_id: editionId,
      subjects: [
        {
          subject_id: subjectId,
          title: "Campagne Iranian Proxy",
          tlp: "AMBER",
          latest_run_id: active || completed ? runId : null,
          latest_run_number: active || completed ? 1 : null,
          latest_status: active ? "running" : completed ? "ready" : null,
          latest_stage: active ? "sources" : completed ? "assembly" : null,
          active_run_id: active ? runId : null,
          can_start: !active,
          blocking_reason: active ? "production_batch_active" : null,
        },
      ],
      active_batch: activeBatch,
      recent_batches: recentBatches,
    };
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
    edition_state: "open",
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
    if (
      productionSurfaceVisited &&
      path.startsWith(`/api/editions/${editionId}/selection`)
    ) {
      throw new Error("Production surface called the Selection API");
    }

    if (path === `/api/editions/${editionId}`) {
      await route.fulfill({ json: edition() });
      return;
    }
    if (
      path === `/api/editions/${editionId}/selection/decisions` &&
      request.method() === "POST"
    ) {
      selectionConfirmed = true;
      await route.fulfill({ json: selectionBoard() });
      return;
    }
    if (path === `/api/editions/${editionId}/selection`) {
      if (productionSurfaceVisited) {
        throw new Error("Production surface called the Selection API");
      }
      await route.fulfill({ json: selectionBoard() });
      return;
    }
    if (
      path === `/api/editions/${editionId}/production/batches` &&
      request.method() === "POST"
    ) {
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      productionPostBody = request.postDataJSON();
      batchStarted = true;
      await route.fulfill({ status: 202, json: runningBatch });
      return;
    }
    if (path === `/api/editions/${editionId}/production`) {
      if (!batchStarted) {
        await route.fulfill({ json: productionBoard(null, []) });
        return;
      }
      batchReads += 1;
      await route.fulfill({
        json:
          batchReads === 1
            ? productionBoard(runningBatch, [])
            : productionBoard(null, [batch]),
      });
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
          edition_state: "open",
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
    if (path === `/api/subjects/${subjectId}`) {
      await route.fulfill({
        json: {
          id: subjectId,
          edition_id: editionId,
          title: "Campagne Iranian Proxy",
          tlp: "AMBER",
          state: "open",
          created_at: "2026-08-29T00:00:00Z",
          updated_at: "2026-08-29T00:00:00Z",
        },
      });
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
    if (path === `/api/subjects/${subjectId}/production/artifacts/extraction`) {
      await route.fulfill({
        json: {
          artifact_id: "extraction-artifact-1",
          stage: "extraction",
          version: 1,
          status: "verified",
          metadata: {},
          rendered_content: null,
          canonical_content: {
            schema_version: 1,
            subject_id: subjectId,
            production_input_hash: hash,
            references_corpus_hash: "b".repeat(64),
            profile_policy_version: "production-reference-tier-v1",
            sources: [
              {
                source_document_id: "12121212-1212-4212-8212-121212121212",
                canonical_url: "https://vendor.example/iranian-proxy",
                title: "Iranian Proxy technical report",
                content_sha256: "c".repeat(64),
                tier: "core",
                kind: "publication",
                role: "primary",
                profile: "full",
                checkpoint_id: "34343434-3434-4434-8434-343434343434",
                reuse_state: "fresh",
                facts: [
                  {
                    category: "actor",
                    value: "Iranian Proxy",
                    context: "Campaign attribution.",
                    evidence_quote: "Iranian Proxy conducted the campaign.",
                    evidence_basis: "exact_quote",
                  },
                ],
                events: [
                  {
                    event_date: "2026-08-12",
                    date_text: "12 August 2026",
                    text: "The campaign was first observed.",
                    context: "Initial activity.",
                    source_document_id: "12121212-1212-4212-8212-121212121212",
                    evidence_quote: "First observed on 12 August 2026.",
                    evidence_basis: "date_and_event_quote",
                  },
                ],
                indicators: [],
                rules: [],
                uncertainties: [],
              },
            ],
            omitted_sources: [],
            warnings: [],
          },
        },
      });
      return;
    }
    await route.fulfill({ status: 404, json: {} });
  });

  // 1. Selection materializes the Subject.
  await page.goto(`/editions/${editionId}/selection`);
  const card = page.getByRole("article").filter({
    has: page.getByRole("heading", {
      name: "Campagne Iranian Proxy",
      exact: true,
    }),
  });
  await card.getByRole("button", { name: "Traiter", exact: true }).click();
  await page
    .getByRole("button", { name: "Confirmer les décisions (1)" })
    .click();
  await expect(card.getByRole("link", { name: "Sujet créé" })).toHaveAttribute(
    "href",
    `/subjects/${subjectId}`,
  );

  // 2. The next-batch choice lives on /production, never on /selection.
  productionSurfaceVisited = true;
  await page.goto(`/editions/${editionId}/production`);
  const selector = page.getByRole("region", {
    name: "Sélecteur du lot de production",
  });
  await selector
    .getByRole("checkbox", { name: "Campagne Iranian Proxy" })
    .check();
  await page
    .getByRole("button", { name: "Démarrer le lot de production" })
    .click();

  await expect(page).toHaveURL(`/editions/${editionId}/production`);
  await expect
    .poll(() => productionPostBody)
    .toEqual({
      subject_ids: [subjectId],
    });
  await expect(
    page.getByRole("heading", { name: "1 / 1 sujets traités" }),
  ).toBeVisible();
  await expect(page.getByText("1 prêts")).toBeVisible();
  await page.getByRole("link", { name: "Revue" }).click();
  await expect(page).toHaveURL(`/editions/${editionId}/review`);

  await expect(
    page.getByRole("heading", { name: "Revue de publication" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Ouvrir" }).click();
  await expect(
    page.getByRole("heading", { name: "Campagne Iranian Proxy" }),
  ).toBeVisible();
  await expect(page.getByText("TLP:AMBER")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Retour à l’édition" }),
  ).toHaveAttribute("href", `/editions/${editionId}`);
  await expect(
    page.getByRole("heading", { name: "Article canonique Iranian Proxy" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Article" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await page.goto(`/subjects/${subjectId}/production/artifacts/extraction`);
  await expect(
    page.getByRole("heading", { name: "Sources FULL" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Iranian Proxy technical report" }),
  ).toBeVisible();
  await expect(
    page.getByText("The campaign was first observed."),
  ).toBeVisible();
  await expect(page.getByText(/schema_version/)).toHaveCount(0);
  await page.goBack();

  await expect(
    page.getByRole("button", { name: "Accepter la production" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Accepter la production" }).click();
  await page.getByRole("link", { name: "Publication" }).click();
  await expect(page).toHaveURL(`/editions/${editionId}/publication`);
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
      `POST /api/editions/${editionId}/production/batches`,
      `GET /api/editions/${editionId}/production`,
      `GET /api/editions/${editionId}/review`,
      `POST /api/editions/${editionId}/publication/accept`,
      `GET /api/editions/${editionId}/release`,
      `GET /api/subjects/${subjectId}/content`,
    ]),
  );
  expect(seenPaths).not.toContain(`POST /api/editions/${editionId}/production`);
  expect(seenPaths.join("\n")).not.toContain("EditorialGroup");
});
