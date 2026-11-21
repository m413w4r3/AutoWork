import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import type { EditorialWorkspaceSnapshot } from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
  fulfillEditorialReadStatuses,
} from "./fixtures/editorialApi";

interface TouchTargetElement {
  getBoundingClientRect: () => {
    height: number;
    left: number;
    right: number;
  };
  tagName: string;
}

const subjectId = EDITORIAL_FIXTURES.subject.id;
const appOrigin = EDITORIAL_APP_ORIGIN;
const titleId = "00000000-0000-4000-8000-000000000000";
const sectionId = "11111111-1111-4111-8111-111111111111";
const paragraphId = "22222222-2222-4222-8222-222222222222";

const snapshot: EditorialWorkspaceSnapshot = {
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
      block_id: titleId,
      kind: "title",
      parent_block_id: null,
      position: 0,
      text: EDITORIAL_FIXTURES.article.title,
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: titleId, legacy_target_id: null },
      before_hash: null,
      allowed_operations: [],
      revision_status: "ready",
    },
    {
      block_id: sectionId,
      kind: "section_heading",
      parent_block_id: null,
      position: 1,
      text: "Contexte",
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: sectionId, legacy_target_id: null },
      before_hash: null,
      allowed_operations: ["rewrite_section", "delete_section"],
      revision_status: "ready",
    },
    {
      block_id: paragraphId,
      kind: "paragraph",
      parent_block_id: sectionId,
      position: 2,
      text: EDITORIAL_FIXTURES.article.paragraphs[0].text,
      evidence_refs: [
        {
          source_title: "Source synthétique",
          passage: "Passage étayant l’occurrence sélectionnée.",
        },
      ],
      chart: null,
      revision_target: { block_id: paragraphId, legacy_target_id: null },
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
    { block_id: sectionId, label: "Contexte", children: [paragraphId] },
  ],
  capabilities: {
    can_discuss: false,
    can_preview_revision: false,
    manual_edit: false,
    can_add_enrichment: false,
    can_preview_pdf: true,
    can_regenerate_pdf: false,
  },
  preview_status: "READY",
  impact_status: "none",
  pdf_status: "CURRENT",
  pdf_artifact_id: null,
  pdf_artifact_version: null,
  pdf_artifact_input_hash: null,
  pdf_artifact_canonical_sha256: null,
  pdf_render_id: null,
  pdf_render_identity: null,
  pdf_error_code: null,
  pdf_error_message: null,
  pdf_url: null,
  snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
};

async function stubEditorApi(
  page: Page,
): Promise<{ unexpectedRequests: string[] }> {
  const unexpectedRequests: string[] = [];
  const subjectPath = `/api/subjects/${subjectId}`;
  const snapshotPath = `${subjectPath}/editor/snapshot`;
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== appOrigin) {
      unexpectedRequests.push(`${method} ${url.origin}${url.pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    if (
      await fulfillEditorialReadStatuses(route, method, url.pathname, snapshot)
    ) {
      return;
    }
    if (
      method === "GET" &&
      /^\/api\/subjects\/[^/]+\/editor\/blocks\/[^/]+\/draft$/.test(
        url.pathname,
      )
    ) {
      await route.fulfill({
        status: 404,
        json: { detail: { code: "draft_not_found" } },
      });
      return;
    }
    if (method === "GET" && url.pathname === snapshotPath) {
      await route.fulfill({ json: snapshot });
      return;
    }
    const threadPrefix = `${subjectPath}/editor/threads/`;
    if (method === "GET" && url.pathname.startsWith(threadPrefix)) {
      const etag = '"playwright-thread-empty"';
      if (route.request().headers()["if-none-match"] === etag) {
        await route.fulfill({ status: 304, headers: { ETag: etag } });
        return;
      }
      const occurrenceId = url.pathname.slice(threadPrefix.length);
      const binding = {
        subject_id: snapshot.subject_id,
        production_run_id: snapshot.production_run_id,
        occurrence_id: occurrenceId,
        artifact_id: snapshot.synthesis_artifact_id,
        artifact_version: snapshot.synthesis_version,
        canonical_sha256: snapshot.synthesis_canonical_sha256,
      };
      const afterSequence = Number(
        url.searchParams.get("after_sequence") ?? "0",
      );
      const timeline: { sequence: number }[] = [];
      await route.fulfill({
        status: 200,
        headers: { ETag: etag },
        json: {
          kind: "unopened",
          binding,
          current: binding,
          bridge_status: "ready",
          event_sequence: 0,
          timeline: timeline.filter((event) => event.sequence > afterSequence),
          etag,
        },
      });
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
    unexpectedRequests.push(`${method} ${url.pathname}`);
    await route.abort("blockedbyclient");
  });
  return { unexpectedRequests };
}

test("un paragraphe garde son contexte exact aux trois largeurs attendues", async ({
  page,
}) => {
  const api = await stubEditorApi(page);
  const screenshotDirectory = resolve(
    process.cwd(),
    "../.autowork-runs/shots-m1-05",
  );
  await mkdir(screenshotDirectory, { recursive: true });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/subjects/${subjectId}/editor`);
  const paragraph = page.locator(`[data-block-id="${paragraphId}"]`);
  await expect(paragraph).toBeVisible();
  await paragraph.click();
  await expect(paragraph).toHaveAttribute("data-selected", "true");
  await expect(page.getByText("Section : Contexte")).toBeVisible();
  await expect(
    page.getByText("Passage étayant l’occurrence sélectionnée."),
  ).toBeVisible();
  await page.screenshot({
    path: resolve(screenshotDirectory, "atelier-1440x900.png"),
  });

  await page.setViewportSize({ width: 1180, height: 800 });
  await expect(page.getByRole("button", { name: "Plan" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Inspecteur" })).toBeHidden();
  await expect(page.getByText("Section : Contexte")).toBeVisible();
  const firstPlanItem = page
    .getByRole("navigation", { name: "Plan éditorial" })
    .getByRole("treeitem")
    .first();
  await expect(firstPlanItem).toHaveAttribute("title", /: .+/);
  await page.screenshot({
    path: resolve(screenshotDirectory, "atelier-1180x800.png"),
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Inspecteur" }).click();
  await expect(
    page.locator(".editorial-workspace__inspector-column"),
  ).toHaveCSS("transform", "none");
  await expect(
    page.getByRole("button", { name: "‹ Retour au document" }),
  ).toBeVisible();
  await expect(page.getByText("Section : Contexte")).toBeVisible();
  const viewportWidth = page.viewportSize()?.width ?? 390;
  const undersizedTouchControls = await page
    .locator(
      ".editorial-workspace a:visible, .editorial-workspace button:visible, .editorial-workspace summary:visible, .editorial-workspace input:visible",
    )
    .evaluateAll(
      (controls, width) =>
        controls.flatMap((control) => {
          const htmlControl = control as unknown as TouchTargetElement;
          const rect = htmlControl.getBoundingClientRect();
          if (rect.right <= 0 || rect.left >= width) return [];
          return rect.height < 44
            ? [`${htmlControl.tagName.toLowerCase()} ${rect.height}px`]
            : [];
        }),
      viewportWidth,
    );
  expect(undersizedTouchControls).toEqual([]);
  const noHorizontalOverflow = await page
    .locator("html")
    .evaluate<boolean, undefined>((root) => {
      const dimensions = root as { scrollWidth: number; clientWidth: number };
      return dimensions.scrollWidth <= dimensions.clientWidth;
    });
  expect(noHorizontalOverflow).toBe(true);
  await page.screenshot({
    path: resolve(screenshotDirectory, "atelier-390x844.png"),
  });

  await page.getByRole("button", { name: "‹ Retour au document" }).click();
  await expect(paragraph).toHaveAttribute("data-selected", "true");
  expect(api.unexpectedRequests).toEqual([]);
});

test("test_all_five_entry_surfaces_use_editorial_label_and_subject_id", async ({
  page,
}) => {
  const editionId = EDITORIAL_FIXTURES.subject.edition_id;
  const briefSubjectId = "subject-brief-editor-e2e";
  const subjectPath = `/api/subjects/${subjectId}`;
  const editionPath = `/api/editions/${editionId}`;
  const unexpectedRequests: string[] = [];
  const subjectContent = {
    subject_id: subjectId,
    run_id: "run-entry-point",
    pipeline_generation: 1,
    artifact_id: "artifact-entry-point",
    artifact_version: 1,
    artifact_input_hash: "f".repeat(64),
    status: "verified",
    schema_version: "4",
    publication_kind: "article",
    canonical_content: {
      schema_version: "4",
      subject_id: subjectId,
      publication_language: "fr",
      title: EDITORIAL_FIXTURES.article.title,
      lead: [],
      sections: [],
      timeline: [],
      indicators: [],
      sources: [],
      uncertainties: [],
      tables: [],
      diagrams: [],
      figures: [],
    },
  };
  const productionStatus = {
    subject_id: subjectId,
    title: EDITORIAL_FIXTURES.article.title,
    status: "ready",
    current_stage: "assembly",
    progress_current: 7,
    progress_total: 7,
    run_id: "run-entry-point",
    pipeline_generation: 1,
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:00Z",
    finished_at: "2026-01-01T00:01:00Z",
    error_code: null,
    error_message: null,
    error_details: null,
    recovery_disposition: "auto",
    warnings: [],
    stages: Object.fromEntries(
      [
        "sources",
        "references",
        "extraction",
        "relevance_projection",
        "synthesis",
        "editorial_enrichment",
        "assembly",
      ].map((stage) => [
        stage,
        {
          status: "succeeded",
          version: null,
          error_code: null,
          error_message: null,
        },
      ]),
    ),
  };
  const reviewItem = {
    position: 1,
    subject_id: subjectId,
    title: EDITORIAL_FIXTURES.article.title,
    run_id: "run-entry-point",
    pipeline_generation: 1,
    run_status: "ready",
    document_artifact_id: "artifact-entry-point",
    document_artifact_version: 1,
    document_input_hash: "f".repeat(64),
    effective_decision_id: null,
    effective_decision: "include",
    included: true,
    blocking: false,
    rejected_indicator_count: 0,
    rejected_rule_count: 0,
    published_rule_count: 0,
    can_retry: false,
    retry_stage: null,
    requires_reconciliation: false,
    reconciliation: null,
    error_code: null,
    error_message: null,
  };
  const productionDocument = {
    schema_version: "5",
    subject_id: subjectId,
    publication_language: "fr",
    title: EDITORIAL_FIXTURES.article.title,
    lead: [],
    sections: [],
    timeline: [],
    indicators: [],
    sources: [],
    uncertainties: [],
    tables: [],
    diagrams: [],
    figures: [],
    references: [],
    original_indicators: [],
    rich_text: {
      schema_version: "1",
      policy_version: "semantic-policy-v1",
      paragraphs: [],
    },
  };
  const briefDocument = {
    ...productionDocument,
    subject_id: briefSubjectId,
    title: EDITORIAL_FIXTURES.brief.title,
  };
  const preview = {
    edition_id: editionId,
    edition_version: 1,
    composition_id: "composition-entry-point",
    composition_version: 1,
    preview_input_hash: "a".repeat(64),
    artifacts: [],
    classifications: [
      {
        position: 1,
        subject_id: subjectId,
        kind: "article",
        number: "01",
        anchor: "article01",
      },
      {
        position: 2,
        subject_id: briefSubjectId,
        kind: "brief",
        number: "01",
        anchor: "brief01",
      },
    ],
    document: {
      schema_version: "2",
      edition: {
        id: editionId,
        country: "France",
        country_code: "FR",
        period_start: "2026-01-01",
        period_end: "2026-01-31",
        tlp: "GREEN",
        languages: ["fr"],
        state: "OPEN",
        version: 1,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
      publications: [
        { position: 1, subject_id: subjectId, document: productionDocument },
        { position: 2, subject_id: briefSubjectId, document: briefDocument },
      ],
    },
    partial: false,
    missing_subject_ids: [],
    deferred_subject_ids: [],
    stale: false,
  };
  const board = {
    edition_id: editionId,
    subjects: [],
    active_batch: {
      batch_id: "batch-entry-point",
      edition_id: editionId,
      status: "completed",
      phase: "initial",
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
          title: EDITORIAL_FIXTURES.article.title,
          run_id: "run-entry-point",
          status: "ready",
          current_stage: "assembly",
          pipeline_generation: 1,
          auto_recovery_count: 0,
          error_code: null,
          error_message: null,
        },
      ],
      created_at: "2026-01-01T00:00:00Z",
      started_at: "2026-01-01T00:00:00Z",
      finished_at: "2026-01-01T00:01:00Z",
    },
    recent_batches: [],
  };

  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== appOrigin) {
      unexpectedRequests.push(`${method} ${url.origin}${url.pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    if (
      await fulfillEditorialReadStatuses(route, method, url.pathname, [
        snapshot,
        { ...snapshot, subject_id: briefSubjectId, pdf_url: null },
      ])
    ) {
      return;
    }
    if (
      method === "GET" &&
      /^\/api\/subjects\/[^/]+\/editor\/blocks\/[^/]+\/draft$/.test(
        url.pathname,
      )
    ) {
      await route.fulfill({
        status: 404,
        json: { detail: { code: "draft_not_found" } },
      });
      return;
    }
    const requestedSnapshot = url.pathname.match(
      /^\/api\/subjects\/([^/]+)\/editor\/snapshot$/,
    );
    if (method === "GET" && requestedSnapshot) {
      const requestedSubjectId = requestedSnapshot[1] ?? subjectId;
      await route.fulfill({
        json: {
          ...snapshot,
          subject_id: requestedSubjectId,
          publication_kind:
            requestedSubjectId === briefSubjectId ? "brief" : "article",
          blocks:
            requestedSubjectId === briefSubjectId
              ? snapshot.blocks.map((block) =>
                  block.kind === "title"
                    ? { ...block, text: EDITORIAL_FIXTURES.brief.title }
                    : block,
                )
              : snapshot.blocks,
        },
      });
    } else if (method === "GET" && url.pathname === subjectPath) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
    } else if (method === "GET" && url.pathname === `${subjectPath}/content`) {
      await route.fulfill({ json: subjectContent });
    } else if (
      method === "GET" &&
      url.pathname === `${subjectPath}/production`
    ) {
      await route.fulfill({ json: productionStatus });
    } else if (
      method === "GET" &&
      url.pathname === `${subjectPath}/production/runs`
    ) {
      await route.fulfill({ json: [] });
    } else if (method === "GET" && url.pathname === editionPath) {
      await route.fulfill({
        json: {
          ...EDITORIAL_FIXTURES.subject,
          id: editionId,
          country: "France",
          country_code: "FR",
          period_start: "2026-01-01",
          period_end: "2026-01-31",
          languages: ["fr"],
          state: "open",
        },
      });
    } else if (
      method === "GET" &&
      url.pathname === `${editionPath}/production`
    ) {
      await route.fulfill({ json: board });
    } else if (method === "GET" && url.pathname === `${editionPath}/review`) {
      await route.fulfill({
        json: {
          edition_id: editionId,
          items: [reviewItem],
          can_accept: false,
          diagnostics: [],
          exclusions: [],
          unresolved_repair_count: 0,
          pending_rebuild_count: 0,
        },
      });
    } else if (
      method === "GET" &&
      url.pathname === `${editionPath}/review/repairs`
    ) {
      await route.fulfill({
        json: {
          summary: {
            unresolved_total: 0,
            sources_to_supply: 0,
            rejected_iocs_to_review: 0,
            rejected_rules_to_review: 0,
            rejected_other_artifacts: 0,
            articles_with_repairs: 0,
            articles_needing_rebuild: 0,
          },
          items: [],
          articles: [],
          next_cursor: null,
        },
      });
    } else if (method === "GET" && url.pathname === `${editionPath}/preview`) {
      await route.fulfill({ json: preview });
    } else {
      unexpectedRequests.push(`${method} ${url.pathname}`);
      await route.abort("blockedbyclient");
    }
  });

  const openEditorialLink = page.getByRole("link", {
    name: "Ouvrir l’atelier éditorial",
  });
  await page.goto(`/subjects/${subjectId}`);
  await expect(openEditorialLink).toHaveCount(2);
  await openEditorialLink.nth(0).click();
  await expect(page).toHaveURL(`${appOrigin}/subjects/${subjectId}/editor`);
  await page.goBack();
  await openEditorialLink.nth(1).click();
  await expect(page).toHaveURL(`${appOrigin}/subjects/${subjectId}/editor`);
  await page.goBack();
  await page.getByRole("button", { name: "Pipeline" }).click();
  const productionLinks = page.getByRole("navigation", {
    name: "Détail des étapes",
  });
  await expect(
    productionLinks.getByRole("link", { name: "Aperçu" }),
  ).toBeVisible();
  await productionLinks
    .getByRole("link", { name: "Ouvrir l’atelier éditorial" })
    .click();
  await expect(page).toHaveURL(`${appOrigin}/subjects/${subjectId}/editor`);

  await page.goto(`/editions/${editionId}/production`);
  await page.getByRole("link", { name: "Ouvrir l’atelier éditorial" }).click();
  await expect(page).toHaveURL(`${appOrigin}/subjects/${subjectId}/editor`);

  await page.goto(`/editions/${editionId}/review`);
  await page.getByRole("link", { name: "Ouvrir l’atelier éditorial" }).click();
  await expect(page).toHaveURL(`${appOrigin}/subjects/${subjectId}/editor`);
  await page.goBack();
  await page.getByRole("button", { name: "Prévisualisation" }).click();
  await expect(page.getByRole("region", { name: "ARTICLES" })).toBeVisible();
  await expect(page.getByRole("region", { name: "BRÈVES" })).toBeVisible();
  await page
    .getByRole("link", { name: "Ouvrir l’atelier éditorial" })
    .last()
    .click();
  await expect(page).toHaveURL(
    `${appOrigin}/subjects/${briefSubjectId}/editor`,
  );
  expect(unexpectedRequests).toEqual([]);
});
