import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
} from "./fixtures/editorialApi";

const subjectId = EDITORIAL_FIXTURES.subject.id;
const snapshotPath = `/api/subjects/${subjectId}/editor/snapshot`;
const appOrigin = EDITORIAL_APP_ORIGIN;

interface ApiCapture {
  unexpectedRequests: string[];
}

const snapshot = {
  subject_id: subjectId,
  production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  publication_kind: "article",
  synthesis_artifact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  synthesis_version: 4,
  synthesis_canonical_sha256: "a".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  publication_version: 3,
  pipeline_generation: 2,
  blocks: [
    {
      block_id: "00000000-0000-4000-8000-000000000000",
      kind: "title",
      parent_block_id: null,
      position: 0,
      text: "Synthetic editorial article",
      evidence_refs: [],
      revision_target: {
        block_id: "00000000-0000-4000-8000-000000000000",
        legacy_target_id: null,
      },
      before_hash: null,
      allowed_operations: [],
      revision_status: "ready",
    },
    {
      block_id: "11111111-1111-4111-8111-111111111111",
      kind: "section_heading",
      parent_block_id: null,
      position: 1,
      text: "Contexte",
      evidence_refs: [],
      revision_target: {
        block_id: "11111111-1111-4111-8111-111111111111",
        legacy_target_id: null,
      },
      before_hash: null,
      allowed_operations: ["rewrite_section", "delete_section"],
      revision_status: "ready",
    },
    {
      block_id: "22222222-2222-4222-8222-222222222222",
      kind: "paragraph",
      parent_block_id: "11111111-1111-4111-8111-111111111111",
      position: 2,
      text: "Le paragraphe synthétique reste sélectionnable.",
      evidence_refs: [
        {
          source_title: "Source synthétique",
          passage: "Passage étayant l’occurrence sélectionnée.",
        },
      ],
      revision_target: {
        block_id: "22222222-2222-4222-8222-222222222222",
        legacy_target_id: null,
      },
      before_hash: null,
      allowed_operations: [
        "rewrite_paragraph",
        "delete_paragraph",
        "insert_paragraph",
      ],
      revision_status: "ready",
    },
  ],
  enrichments: [],
  document_outline: [
    {
      block_id: "11111111-1111-4111-8111-111111111111",
      label: "Contexte",
      children: ["22222222-2222-4222-8222-222222222222"],
    },
  ],
  capabilities: {
    can_discuss: false,
    can_preview_revision: false,
    manual_edit: false,
    can_add_enrichment: false,
    can_preview_pdf: true,
  },
  preview_status: "READY",
  snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
} as const;

async function stubEditorApi(
  page: Page,
  snapshotStatus = 200,
): Promise<ApiCapture> {
  const capture: ApiCapture = { unexpectedRequests: [] };
  const subjectPath = `/api/subjects/${subjectId}`;
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== appOrigin) {
      capture.unexpectedRequests.push(`${method} ${url.origin}${url.pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    if (method === "GET" && url.pathname === snapshotPath) {
      await route.fulfill(
        snapshotStatus === 200
          ? { json: snapshot }
          : {
              status: snapshotStatus,
              json: {
                detail: {
                  code:
                    snapshotStatus === 403
                      ? "subject_forbidden"
                      : "subject_missing",
                },
              },
            },
      );
      return;
    }
    if (method === "GET" && url.pathname === subjectPath) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
      return;
    }
    if (method === "GET" && url.pathname === `${subjectPath}/content`) {
      await route.fulfill({ status: 404, body: "" });
      return;
    }
    capture.unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });
  return capture;
}

test("la route directe précède le détail sujet et le retour suit l’historique", async ({
  page,
}) => {
  const api = await stubEditorApi(page);

  await page.goto(`/subjects/${subjectId}/editor`);
  await expect(page).toHaveURL(`${appOrigin}/subjects/${subjectId}/editor`);
  await expect(
    page.getByRole("heading", {
      name: "Synthetic editorial article",
      level: 1,
    }),
  ).toBeVisible();
  await page.getByRole("link", { name: "← Retour au sujet" }).click();
  await expect(
    page.getByRole("navigation", { name: "Contenu du sujet" }),
  ).toBeVisible();
  await page.goBack();
  await expect(
    page.getByRole("navigation", { name: "Plan éditorial" }),
  ).toBeVisible();

  expect(api.unexpectedRequests).toEqual([]);
});

for (const status of [403, 404]) {
  test(`un sujet refusé ou absent (${status}) garde un état sûr`, async ({
    page,
  }) => {
    await stubEditorApi(page, status);

    await page.goto(`/subjects/${subjectId}/editor`);
    await expect(
      page.getByRole("alert", { name: "Atelier éditorial indisponible" }),
    ).toHaveText("Cet atelier n’est pas disponible.");
    await expect(page.getByText(subjectId)).toHaveCount(0);
    await expect(page.getByText("Synthetic editorial article")).toHaveCount(0);
  });
}
