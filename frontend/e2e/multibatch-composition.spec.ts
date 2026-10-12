import { expect, test } from "@playwright/test";

const editionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const subjectIds = {
  A: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  B: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbc",
  C: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbd",
} as const;
const batchIds = {
  A: "cccccccc-cccc-4ccc-8ccc-ccccccccccca",
  B: "cccccccc-cccc-4ccc-8ccc-cccccccccccb",
  C: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
} as const;
const runIds = {
  A: "dddddddd-dddd-4ddd-8ddd-ddddddddddda",
  B: "dddddddd-dddd-4ddd-8ddd-dddddddddddb",
  C: "dddddddd-dddd-4ddd-8ddd-dddddddddddc",
} as const;
const artifactIds = {
  A: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeea",
  B: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeeb",
  C: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeec",
} as const;
const documentHash = "a".repeat(64);
const inputHashes = {
  A: "b".repeat(64),
  B: "c".repeat(64),
  C: "d".repeat(64),
} as const;

const subjects = (["A", "B", "C"] as const).map((key) => ({
  id: subjectIds[key],
  edition_id: editionId,
  title: `Sujet ${key}`,
  slug: `sujet-${key.toLowerCase()}`,
  tlp: "AMBER",
  version: 1,
  created_at: "2026-10-12T09:00:00Z",
  updated_at: "2026-10-12T09:00:00Z",
}));

const publicationDocument = (key: "A" | "B" | "C") => ({
  schema_version: "4",
  subject_id: subjectIds[key],
  publication_language: "fr",
  title: `Article ${key}`,
  lead: [{ text: `Contenu ${key}.`, evidence_refs: [] }],
  sections: [],
  timeline: [],
  indicators: [],
  sources: [],
  uncertainties: [],
  tables: [],
  diagrams: [],
  figures: [],
});

test("compose plusieurs batches, prévisualise partiellement et accepte après résolution", async ({
  page,
}) => {
  test.slow();
  let compositionVersion = 0;
  let compositionEntries: Array<Record<string, unknown>> = [];
  let rejectNextCompositionWrite = false;
  const observedPutRequests: Array<{
    expected_version: number;
    entries: Array<Record<string, unknown>>;
    idempotencyKey: string | null;
  }> = [];
  let acceptedCompositionVersion: number | null = null;

  const batch = (key: "A" | "B" | "C") => ({
    batch_id: batchIds[key],
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
        subject_id: subjectIds[key],
        title: `Sujet ${key}`,
        run_id: runIds[key],
        status: "ready",
        current_stage: "assembly",
        pipeline_generation: 4,
        auto_recovery_count: 0,
        error_code: null,
        error_message: null,
      },
    ],
    created_at: "2026-10-12T09:00:00Z",
    started_at: "2026-10-12T09:01:00Z",
    finished_at: "2026-10-12T09:05:00Z",
  });

  const batchBoard = {
    edition_id: editionId,
    subjects: (["A", "B", "C"] as const).map((key) => ({
      subject_id: subjectIds[key],
      title: `Sujet ${key}`,
      tlp: "AMBER",
      latest_run_id: runIds[key],
      latest_run_number: 1,
      latest_status: "ready",
      latest_stage: "assembly",
      active_run_id: null,
      can_start: true,
      blocking_reason: null,
    })),
    active_batch: null,
    recent_batches: [batch("A"), batch("B"), batch("C")],
  };

  const compositionRead = () => ({
    edition_id: editionId,
    composition:
      compositionVersion === 0
        ? null
        : {
            id: "composition-1",
            edition_id: editionId,
            version: compositionVersion,
            actor_id: "analyst@example.test",
            preparation_status: "ready",
            created_at: "2026-10-12T09:10:00Z",
            updated_at: "2026-10-12T09:10:00Z",
            entries: compositionEntries,
          },
    reasons:
      compositionVersion === 0
        ? [
            {
              code: "composition_missing",
              message: "La composition n’est pas encore créée.",
              subject_id: null,
              details: null,
            },
          ]
        : [],
    axes: {
      production_validity: true,
      discovery_update_available: true,
      composition_eligibility: true,
    },
  });

  const validation = () => {
    const included = compositionEntries
      .filter((entry) => entry.disposition === "included")
      .sort(
        (left, right) =>
          Number(left.position ?? 0) - Number(right.position ?? 0),
      );
    const deferred = compositionEntries.filter(
      (entry) => entry.disposition === "deferred",
    );
    const items = included.map((entry) => {
      const key = (["A", "B", "C"] as const).find(
        (candidate) => subjectIds[candidate] === entry.subject_id,
      )!;
      return {
        position: Number(entry.position),
        subject_id: subjectIds[key],
        title: `Sujet ${key}`,
        run_id: runIds[key],
        pipeline_generation: 4,
        run_status: "ready",
        document_artifact_id: artifactIds[key],
        document_artifact_version: 1,
        document_input_hash: inputHashes[key],
        effective_decision_id: null,
        effective_decision: "include",
        included: true,
        blocking: false,
        can_retry: false,
        retry_stage: null,
        requires_reconciliation: false,
        reconciliation: null,
        error_code: null,
        error_message: null,
        superseded_artifact_available: false,
        pinned_generation_current: true,
        latest_artifact_run_id: runIds[key],
        latest_artifact_id: artifactIds[key],
        latest_artifact_version: 1,
      };
    });
    return {
      edition_id: editionId,
      items,
      can_accept:
        compositionVersion > 0 &&
        included.length === 3 &&
        deferred.length === 0,
      composition_version: compositionVersion || null,
      diagnostics: deferred.map((entry) => ({
        code: "subject_deferred",
        subject_id: entry.subject_id,
        disposition: "deferred",
        run_id: null,
        details: { reason: "Article reporté par l’analyste." },
        active_repair_count: 0,
        unresolved_repair_count: 0,
        pending_rebuild_count: 0,
      })),
    };
  };

  const preview = () => {
    const included = compositionEntries
      .filter((entry) => entry.disposition === "included")
      .sort(
        (left, right) =>
          Number(left.position ?? 0) - Number(right.position ?? 0),
      );
    const deferred = compositionEntries
      .filter((entry) => entry.disposition === "deferred")
      .map((entry) => String(entry.subject_id));
    return {
      edition_id: editionId,
      edition_version: 2,
      composition_id: "composition-1",
      composition_version: compositionVersion,
      preview_input_hash: "f".repeat(64),
      artifacts: included.map((entry) => ({
        position: Number(entry.position),
        subject_id: String(entry.subject_id),
        artifact_id: String(entry.artifact_id),
        artifact_version: Number(entry.artifact_version),
        input_hash: String(entry.artifact_input_hash),
      })),
      document: {
        schema_version: "2",
        edition: {
          id: editionId,
          country: "France",
          country_code: "FR",
          period_start: "2026-10-01",
          period_end: "2026-10-31",
          tlp: "AMBER",
          languages: ["fr"],
          state: "OPEN",
          version: 2,
          created_at: "2026-10-12T09:00:00Z",
          updated_at: "2026-10-12T09:00:00Z",
        },
        publications: included.map((entry) => {
          const key = (["A", "B", "C"] as const).find(
            (candidate) => subjectIds[candidate] === entry.subject_id,
          )!;
          return {
            position: Number(entry.position),
            subject_id: subjectIds[key],
            document: publicationDocument(key),
          };
        }),
      },
      partial: deferred.length > 0,
      missing_subject_ids: [],
      deferred_subject_ids: deferred,
      stale: false,
    };
  };

  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const method = request.method();

      if (path === `/api/editions/${editionId}` && method === "GET") {
        await route.fulfill({
          json: {
            id: editionId,
            country: "France",
            country_code: "FR",
            period_start: "2026-10-01",
            period_end: "2026-10-31",
            tlp: "AMBER",
            languages: ["fr"],
            state: "open",
            version: 2,
            created_at: "2026-10-12T09:00:00Z",
            updated_at: "2026-10-12T09:00:00Z",
          },
        });
        return;
      }
      if (path === `/api/editions/${editionId}/subjects` && method === "GET") {
        await route.fulfill({ json: subjects });
        return;
      }
      if (
        path === `/api/editions/${editionId}/production` &&
        method === "GET"
      ) {
        await route.fulfill({ json: batchBoard });
        return;
      }
      if (path === `/api/editions/${editionId}/composition/validation`) {
        await route.fulfill({ json: validation() });
        return;
      }
      if (path === `/api/editions/${editionId}/composition/preview`) {
        await route.fulfill({ json: preview() });
        return;
      }
      if (
        path === `/api/editions/${editionId}/composition` &&
        method === "GET"
      ) {
        await route.fulfill({ json: compositionRead() });
        return;
      }
      if (
        path === `/api/editions/${editionId}/composition` &&
        method === "PUT"
      ) {
        const body = request.postDataJSON() as {
          expected_version: number;
          entries: Array<Record<string, unknown>>;
        };
        observedPutRequests.push({
          ...body,
          idempotencyKey: request.headers()["idempotency-key"] ?? null,
        });
        if (rejectNextCompositionWrite) {
          rejectNextCompositionWrite = false;
          compositionVersion += 1;
          await route.fulfill({
            status: 409,
            json: {
              detail: {
                code: "composition_version_conflict",
                message: "La composition a changé.",
                expected_version: body.expected_version,
                actual_version: compositionVersion,
              },
            },
          });
          return;
        }
        if (body.expected_version !== compositionVersion) {
          await route.fulfill({
            status: 409,
            json: {
              detail: {
                code: "composition_version_conflict",
                message: "La composition a changé.",
                expected_version: body.expected_version,
                actual_version: compositionVersion,
              },
            },
          });
          return;
        }
        compositionVersion += 1;
        compositionEntries = body.entries.map((entry) => ({
          id: `entry-${String(entry.subject_id).slice(-4)}`,
          preparation_status: "ready",
          ...entry,
        }));
        await route.fulfill({
          json: {
            id: "composition-1",
            edition_id: editionId,
            version: compositionVersion,
            actor_id: "analyst@example.test",
            preparation_status: "ready",
            created_at: "2026-10-12T09:10:00Z",
            updated_at: "2026-10-12T09:10:00Z",
            entries: compositionEntries,
          },
        });
        return;
      }
      if (
        path === `/api/editions/${editionId}/composition/accept` &&
        method === "POST"
      ) {
        const body = request.postDataJSON() as {
          expected_composition_version: number;
        };
        acceptedCompositionVersion = body.expected_composition_version;
        await route.fulfill({
          status: 202,
          json: {
            edition_id: editionId,
            edition_state: "open",
            manifest_id: "manifest-1",
            manifest_sha256: documentHash,
            manifest_schema_version: "2",
            edition_version: 3,
            composition_id: "composition-1",
            composition_version: body.expected_composition_version,
            job_id: "job-1",
            job_dispatched: true,
          },
        });
        return;
      }
      if (path.endsWith("/production/runs") && method === "GET") {
        const subjectId = path.split("/")[3];
        const key = (["A", "B", "C"] as const).find(
          (candidate) => subjectIds[candidate] === subjectId,
        );
        await route.fulfill({
          json: key
            ? [
                {
                  run_id: runIds[key],
                  edition_id: editionId,
                  subject_id: subjectId,
                  run_number: 1,
                  status: "ready",
                  current_stage: "assembly",
                  pipeline_generation: 4,
                  research_date: "2026-10-12",
                  created_at: "2026-10-12T09:00:00Z",
                  started_at: "2026-10-12T09:01:00Z",
                  finished_at: "2026-10-12T09:05:00Z",
                  error_code: null,
                  error_message: null,
                },
              ]
            : [],
        });
        return;
      }
      if (
        path.endsWith("/production/artifacts/publication") &&
        method === "GET"
      ) {
        const subjectId = path.split("/")[3];
        const key = (["A", "B", "C"] as const).find(
          (candidate) => subjectIds[candidate] === subjectId,
        );
        await route.fulfill({
          json: key
            ? {
                artifact_id: artifactIds[key],
                production_run_id: runIds[key],
                stage: "publication",
                version: 1,
                input_hash: inputHashes[key],
                canonical_sha256: documentHash,
                status: "verified",
                metadata: {},
                canonical_content: publicationDocument(key),
              }
            : {},
        });
        return;
      }
      await route.fulfill({ status: 404, json: { detail: "Not found" } });
    },
  );

  await page.goto(`/editions/${editionId}/composition`);
  await expect(
    page.getByRole("heading", { name: "Sujets et ordre de publication" }),
  ).toBeVisible();
  await expect(
    page.getByText(/Une mise à jour Discovery est disponible/),
  ).toBeVisible();

  for (const key of ["A", "B", "C"] as const) {
    const card = page.locator(".composition-console__subject").filter({
      has: page.getByRole("heading", { name: `Sujet ${key}` }),
    });
    await expect(card).toContainText("Production : Prête");
    await expect(card).toContainText(batchIds[key].slice(0, 8));
    await card.getByRole("button", { name: "Inclure" }).click();
    await expect(card.getByText(/Inclus · position/)).toBeVisible();
  }
  expect(observedPutRequests).toHaveLength(3);
  expect(observedPutRequests.every((item) => item.idempotencyKey)).toBe(true);
  expect(observedPutRequests.map((item) => item.expected_version)).toEqual([
    0, 1, 2,
  ]);

  const cardB = page.locator(".composition-console__subject").filter({
    has: page.getByRole("heading", { name: "Sujet B" }),
  });
  const cardC = page.locator(".composition-console__subject").filter({
    has: page.getByRole("heading", { name: "Sujet C" }),
  });
  await cardC.getByRole("button", { name: "Monter Sujet C" }).click();
  await expect(cardC.getByText("Inclus · position 2")).toBeVisible();
  await cardB.getByRole("button", { name: "Reporter" }).click();
  await expect(cardB.getByText("Reporté", { exact: true })).toBeVisible();

  await page
    .getByRole("button", {
      name: "Prévisualiser la composition (aperçu potentiellement partiel)",
    })
    .click();
  await expect(page.getByText(/Aperçu partiel/)).toBeVisible();
  await expect(
    page.getByText(new RegExp(`Sujets reportés : ${subjectIds.B}`)),
  ).toBeVisible();
  await expect(page.getByRole("checkbox")).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Accepter la composition" }),
  ).toBeDisabled();
  await expect(page.getByText("subject_deferred").first()).toBeVisible();

  rejectNextCompositionWrite = true;
  await cardC.getByRole("button", { name: "Monter Sujet C" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "votre modification n’a pas été réappliquée",
  );
  await page.getByRole("button", { name: "Recharger la composition" }).click();
  await expect(page.locator(".composition-console__heading")).toContainText(
    "Version 6",
  );
  await cardC.getByRole("button", { name: "Monter Sujet C" }).click();
  await expect(cardC.getByText("Inclus · position 1")).toBeVisible();

  await cardB.getByRole("button", { name: "Inclure" }).click();
  await expect(cardB.getByText("Inclus · position 3")).toBeVisible();
  await expect(page.getByRole("checkbox")).toBeEnabled();
  await page.getByRole("checkbox").check();
  await page.getByRole("button", { name: "Accepter la composition" }).click();
  await expect.poll(() => acceptedCompositionVersion).toBe(compositionVersion);
  expect(acceptedCompositionVersion).toBe(8);
});
