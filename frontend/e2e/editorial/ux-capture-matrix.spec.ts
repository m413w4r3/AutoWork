import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";
import type { Locator, Page, Route } from "@playwright/test";

import type {
  EditorialEvidenceReference,
  EditorialFigureCatalogItem,
  EditorialHistoryItem,
  EditorialProposalRead,
  EditorialThreadBinding,
  EditorialThreadOpenRead,
  EditorialThreadRead,
  EditorialThreadTimelineEvent,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
  fulfillEditorialReadStatuses,
} from "./fixtures/editorialApi";

/**
 * F6-A desktop UX capture matrix.
 *
 * One synthetic, fully stubbed workshop (no live provider, no network) is
 * captured at the four supported desktop widths for nineteen review states.
 * Every capture is also a regression assertion: page overflow, raw technical
 * strings, accessible names, and control overlap are checked before the PNG is
 * written.
 *
 * The real UI is exercised; only the HTTP API is stubbed, exactly like the
 * existing visual-review and shots specs.
 */

const subjectId = EDITORIAL_FIXTURES.subject.id;
const runId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const artifactId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const canonicalSha = "a".repeat(64);
const snapshotId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const threadId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const paragraphProposalId = "11111111-1111-4111-8111-111111111111";
const chartProposalId = "22222222-2222-4222-8222-222222222222";
const historicalArtifactId = "99999999-9999-4999-8999-999999999999";
const publicationId = "abababab-abab-4bab-8bab-abababababab";

const titleId = "30000000-0000-4000-8000-000000000001";
const headingId = "30000000-0000-4000-8000-000000000002";
const leadId = "30000000-0000-4000-8000-000000000003";
const paragraphId = "30000000-0000-4000-8000-000000000004";
const chartBlockId = "30000000-0000-4000-8000-000000000005";
const figureAId = "30000000-0000-4000-8000-000000000006";
const figureBId = "30000000-0000-4000-8000-000000000007";
const secondParagraphId = "30000000-0000-4000-8000-000000000008";
const thirdParagraphId = "30000000-0000-4000-8000-000000000009";

const CHART_KEY = "ux-chart-volumes";
const FIGURE_A_KEY = "ux-figure-a";
const FIGURE_B_KEY = "ux-figure-b";

const figureIds = {
  a1: "40000000-0000-4000-8000-000000000001",
  a2: "40000000-0000-4000-8000-000000000002",
  b1: "40000000-0000-4000-8000-000000000003",
  b2: "40000000-0000-4000-8000-000000000004",
  a3: "40000000-0000-4000-8000-000000000005",
} as const;
const sourceA = "50000000-0000-4000-8000-000000000001";
const sourceB = "50000000-0000-4000-8000-000000000002";

const paragraphText = "Le paragraphe décrit la mise en page et les sources.";
const proposedParagraph =
  "Le paragraphe montre la mise en page et les sources.";
const secondParagraphText =
  "Un second paragraphe synthétique apporte de la longueur au document et aide à juger la place laissée au contenu éditorial.";
const thirdParagraphText =
  "La dernière phrase reste fictive et sert uniquement à observer la hiérarchie visuelle de l’atelier.";
const conflictMessage =
  "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition.";

const matrixRoot = resolve(process.cwd(), "../.autowork-runs/ux-matrix");

interface Viewport {
  width: number;
  height: number;
}

const VIEWPORTS: Viewport[] = [
  { width: 1024, height: 800 },
  { width: 1280, height: 900 },
  { width: 1440, height: 1000 },
  { width: 1920, height: 1080 },
];

interface ScenarioConfig {
  kind?: "article" | "brief";
  pdfStale?: boolean;
  paragraphThread?: "discussion" | "proposal" | "conflict";
  chartProposal?: boolean;
  historical?: boolean;
}

interface ScenarioRuntime {
  unexpectedRequests: string[];
}

/* ------------------------------------------------------------------ */
/* Synthetic snapshot                                                  */
/* ------------------------------------------------------------------ */

function evidence(): EditorialEvidenceReference {
  return {
    source_document_id: "source-synthetic-1",
    kind: "fact",
    evidence_key: "synthetic-fact-1",
  };
}

function block(
  blockId: string,
  kind: EditorialWorkspaceBlock["kind"],
  position: number,
  text: string,
  overrides: Partial<EditorialWorkspaceBlock> = {},
): EditorialWorkspaceBlock {
  return {
    block_id: blockId,
    kind,
    parent_block_id: headingId,
    position,
    text,
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: canonicalSha,
    allowed_operations: [],
    revision_status: "ready",
    ...overrides,
  };
}

function chartBlock(): EditorialWorkspaceBlock {
  return block(chartBlockId, "chart", 4, "Graphique des volumes observés", {
    parent_block_id: null,
    evidence_refs: [{ label: "Source synthétique du graphique" }],
    allowed_operations: [
      "update_visual_caption",
      "move_enrichment_element",
      "rewrite_chart",
      "delete_chart",
      "reorder_enrichment_elements",
    ],
    element_kind: "chart",
    element_key: CHART_KEY,
    order: 0,
    caption: "Volumes observés dans les sources synthétiques.",
    chart: {
      key: CHART_KEY,
      kind: "bar",
      title: "Graphique des volumes observés",
      caption: "Volumes observés dans les sources synthétiques.",
      legend: ["Série A"],
      category_axis_label: "Catégorie",
      value_axis_label: "Valeur",
      unit: "unités",
      points: [
        { label: "Catégorie 1", value: "12", evidence_refs: [evidence()] },
        { label: "Catégorie 2", value: "7", evidence_refs: [] },
      ],
      anchor_block_id: paragraphId,
    },
  });
}

function figureBlock(
  blockId: string,
  key: string,
  caption: string,
  order: number,
  catalogFigureId: string,
  position: number,
): EditorialWorkspaceBlock {
  return block(blockId, "figure", position, caption, {
    parent_block_id: null,
    evidence_refs: [{ label: `Source synthétique de ${caption}` }],
    allowed_operations: [
      "update_visual_caption",
      "move_enrichment_element",
      "replace_source_figure",
      "delete_source_figure",
      "reorder_enrichment_elements",
    ],
    element_kind: "figure",
    element_key: key,
    order,
    caption,
    figure: {
      key,
      catalog_figure_id: catalogFigureId,
      original_caption: `Légende archivée de ${caption}`,
      caption,
      source_url: "https://publisher.example/synthetic-source",
      source: "Rapport public synthétique",
      provenance:
        "Figure archivée et vérifiée dans une source synthétique, conservée dans l’inventaire.",
      sha256: "b".repeat(64),
      mime_type: "image/jpeg",
      byte_size: 4096,
      width: 1280,
      height: 720,
      status: "included",
      order,
      anchor_block_id: paragraphId,
      placement: "after_paragraph",
      page: 2,
      section: "Contexte",
    },
  });
}

function makeSnapshot(config: ScenarioConfig): EditorialWorkspaceSnapshot {
  const kind = config.kind ?? "article";
  const stalePdf = config.pdfStale ?? false;
  // A brief fixture starts without integrated figures so the 3-figure ceiling
  // is expressed by the gallery itself, not by pre-existing content.
  const includeFigures = kind === "article";
  const blocks: EditorialWorkspaceBlock[] = [
    block(titleId, "title", 0, "Bulletin synthétique M23", {
      parent_block_id: null,
      allowed_operations: ["rewrite_title"],
    }),
    block(headingId, "section_heading", 1, "Contexte", {
      parent_block_id: null,
      allowed_operations: ["rename_section", "discuss"],
    }),
    block(leadId, "lead_paragraph", 2, "Chapeau synthétique de l’article.", {
      allowed_operations: ["rewrite_paragraph", "delete_paragraph"],
    }),
    block(paragraphId, "paragraph", 3, paragraphText, {
      evidence_refs: [
        {
          source_title: "Rapport public synthétique",
          passage:
            "Passage de démonstration qui étaye le paragraphe sélectionné.",
        },
      ],
      allowed_operations: [
        "rewrite_paragraph",
        "discuss",
        "insert_after",
        "create_table",
        "create_bar_chart",
        "create_source_figure",
      ],
    }),
    chartBlock(),
  ];
  if (includeFigures) {
    blocks.push(
      figureBlock(
        figureAId,
        FIGURE_A_KEY,
        "Figure A — échanges observés",
        1,
        figureIds.a1,
        5,
      ),
      figureBlock(
        figureBId,
        FIGURE_B_KEY,
        "Figure B — infrastructure",
        2,
        figureIds.b1,
        6,
      ),
    );
  }
  blocks.push(
    block(secondParagraphId, "paragraph", 7, secondParagraphText, {
      allowed_operations: ["rewrite_paragraph"],
    }),
    block(thirdParagraphId, "paragraph", 8, thirdParagraphText, {
      allowed_operations: ["rewrite_paragraph"],
    }),
  );
  const childIds = [
    paragraphId,
    chartBlockId,
    ...(includeFigures ? [figureAId, figureBId] : []),
    secondParagraphId,
    thirdParagraphId,
  ];
  return {
    subject_id: subjectId,
    production_run_id: runId,
    publication_kind: kind,
    synthesis_artifact_id: artifactId,
    synthesis_version: 4,
    synthesis_canonical_sha256: canonicalSha,
    enrichment_artifact_id: "70000000-0000-4000-8000-000000000001",
    enrichment_version: 2,
    publication_artifact_id: stalePdf ? null : publicationId,
    publication_version: stalePdf ? null : 4,
    pipeline_generation: 1,
    blocks,
    enrichments: blocks.filter((candidate) => candidate.element_kind),
    document_outline: [
      {
        block_id: headingId,
        label: "Contexte",
        children: childIds,
      },
    ],
    capabilities: {
      can_discuss: true,
      can_preview_revision: false,
      manual_edit: true,
      can_add_enrichment: true,
      can_preview_pdf: true,
      can_regenerate_pdf: true,
      read_only_reason: null,
    },
    preview_status: stalePdf ? "STALE" : "READY",
    impact_status: "none",
    pdf_status: stalePdf ? "STALE" : "CURRENT",
    pdf_artifact_id: stalePdf ? null : publicationId,
    pdf_artifact_version: stalePdf ? null : 4,
    pdf_artifact_input_hash: stalePdf ? null : "c".repeat(64),
    pdf_artifact_canonical_sha256: stalePdf ? null : "d".repeat(64),
    pdf_render_id: stalePdf ? null : "80000000-0000-4000-8000-000000000001",
    pdf_render_identity: stalePdf ? null : "synthetic-render-v4",
    pdf_error_code: null,
    pdf_error_message: null,
    pdf_url: stalePdf
      ? null
      : `/api/subjects/${subjectId}/editor/pdf/download?version=4`,
    snapshot_id: snapshotId,
  };
}

/* ------------------------------------------------------------------ */
/* Synthetic threads and proposals                                     */
/* ------------------------------------------------------------------ */

function binding(occurrenceId: string): EditorialThreadBinding {
  return {
    subject_id: subjectId,
    production_run_id: runId,
    occurrence_id: occurrenceId,
    artifact_id: artifactId,
    artifact_version: 4,
    canonical_sha256: canonicalSha,
  };
}

function timelineEvent(
  id: string,
  sequence: number,
  kind: EditorialThreadTimelineEvent["kind"],
  body: string | null,
  proposal: string | null = null,
): EditorialThreadTimelineEvent {
  return {
    id,
    sequence,
    kind,
    actor_id: null,
    proposal_id: proposal,
    created_at: "2026-11-22T10:00:00Z",
    body,
  };
}

function openThread(
  occurrenceId: string,
  timeline: EditorialThreadTimelineEvent[],
  tag: string,
): EditorialThreadOpenRead {
  return {
    kind: "open",
    thread_id: threadId,
    origin: binding(occurrenceId),
    current: binding(occurrenceId),
    binding_state: "valid",
    previous_sessions: [],
    bridge_status: "reply_available",
    event_sequence: timeline.length,
    timeline,
    etag: `"ux-${tag}"`,
  };
}

function threadFor(
  occurrenceId: string,
  config: ScenarioConfig,
): EditorialThreadRead {
  if (
    occurrenceId === paragraphId &&
    (config.paragraphThread === "discussion" ||
      config.paragraphThread === "proposal" ||
      config.paragraphThread === "conflict")
  ) {
    const timeline = [
      timelineEvent(
        "90000000-0000-4000-8000-000000000001",
        1,
        "user_message",
        "Raccourcis ce passage sans perdre les faits établis.",
      ),
    ];
    if (config.paragraphThread !== "discussion") {
      timeline.push(
        timelineEvent(
          "90000000-0000-4000-8000-000000000002",
          2,
          "proposal",
          null,
          paragraphProposalId,
        ),
      );
    } else {
      timeline.push(
        timelineEvent(
          "90000000-0000-4000-8000-000000000003",
          2,
          "bridge_reply",
          "Voici une reformulation qui conserve les faits documentés.",
        ),
      );
    }
    return openThread(
      occurrenceId,
      timeline,
      config.paragraphThread === "discussion" ? "discussion" : "proposal",
    );
  }
  if (occurrenceId === chartBlockId && config.chartProposal) {
    return openThread(
      occurrenceId,
      [
        timelineEvent(
          "90000000-0000-4000-8000-000000000004",
          1,
          "user_message",
          "Corrige le titre et la première valeur du graphique.",
        ),
        timelineEvent(
          "90000000-0000-4000-8000-000000000005",
          2,
          "proposal",
          null,
          chartProposalId,
        ),
      ],
      "chart-proposal",
    );
  }
  return {
    kind: "unopened",
    binding: binding(occurrenceId),
    current: binding(occurrenceId),
    bridge_status: "ready",
    event_sequence: 0,
    timeline: [],
    etag: `"ux-unopened-${occurrenceId}"`,
  };
}

function paragraphProposal(): EditorialProposalRead {
  return {
    id: paragraphProposalId,
    subject_id: subjectId,
    production_run_id: runId,
    occurrence_id: paragraphId,
    thread_id: threadId,
    origin: "bridge_draft",
    state: "proposed",
    state_version: 1,
    operation: "rewrite_paragraph",
    base_artifact_id: artifactId,
    base_artifact_version: 4,
    base_canonical_sha256: canonicalSha,
    base_text: paragraphText,
    patch_sha256: "e".repeat(64),
    patch: {
      schema_version: "editorial-patch-v1",
      operation: "rewrite_paragraph",
      target: { block_id: paragraphId },
      target_id: paragraphId,
      occurrence_id: paragraphId,
      before_hash: canonicalSha,
      rationale:
        "Reformulation plus directe, sans retirer de fait documenté par les sources.",
      after: proposedParagraph,
    },
    diagnostics: {},
    created_at: "2026-11-22T10:01:00Z",
  };
}

function chartProposal(): EditorialProposalRead {
  return {
    id: chartProposalId,
    subject_id: subjectId,
    production_run_id: runId,
    occurrence_id: chartBlockId,
    thread_id: threadId,
    origin: "bridge_draft",
    state: "proposed",
    state_version: 1,
    operation: "rewrite_chart",
    base_artifact_id: artifactId,
    base_artifact_version: 4,
    base_canonical_sha256: canonicalSha,
    base_text: null,
    patch_sha256: "f".repeat(64),
    patch: {
      schema_version: "editorial-enrichment-visual-patch-v1",
      operation: "rewrite_chart",
      target: { element_kind: "chart", element_key: CHART_KEY },
      target_id: CHART_KEY,
      occurrence_id: chartBlockId,
      before_hash: canonicalSha,
      rationale:
        "Le titre et la première valeur sont alignés sur les sources vérifiées.",
      visual_patch: {
        kind: "revise_chart",
        element_key: CHART_KEY,
        chart: {
          key: CHART_KEY,
          kind: "bar",
          title: "Volumes corrigés après revue",
          caption: "Volumes corrigés dans les sources synthétiques.",
          placement: {
            kind: "after_paragraph",
            section_index: null,
            anchor_block_id: paragraphId,
          },
          purpose: {
            question: "Que montrent les volumes observés ?",
            available_data: "Deux catégories étayées par les sources.",
            comprehension_gain: "Compare les volumes observés.",
            scope: "Périmètre synthétique de la revue.",
            evidence_refs: [evidence()],
            knowledge_limits: "Limites synthétiques connues.",
            placement_reason: "Après le paragraphe d’ancrage.",
          },
          category_axis_label: "Catégorie",
          value_axis_label: "Valeur",
          unit: "unités",
          points: [
            {
              label: "Catégorie 1",
              series: "Série A",
              value: "4",
              evidence_refs: [evidence()],
            },
            {
              label: "Catégorie 2",
              series: "Série A",
              value: "7",
              evidence_refs: [],
            },
          ],
        },
      },
    },
    diagnostics: {},
    created_at: "2026-11-22T10:01:00Z",
  };
}

/* ------------------------------------------------------------------ */
/* Figure catalog                                                      */
/* ------------------------------------------------------------------ */

function catalogItem(
  figureId: string,
  name: string,
  sourceId: string,
  sourceTitle: string,
  status: EditorialFigureCatalogItem["status"],
  selectable: boolean,
  exclusionReason: string | null = null,
): EditorialFigureCatalogItem {
  return {
    figure_id: figureId,
    original_name: name,
    original_caption: `Légende archivée de ${name}`,
    alt_text: name,
    source: {
      source_id: sourceId,
      title: sourceTitle,
      url: "https://publisher.example/synthetic-source",
    },
    page: 3,
    section: "Contexte",
    context_before: "Contexte synthétique précédent conservé dans l’archive.",
    context_after: "Contexte synthétique suivant conservé dans l’archive.",
    width: 1280,
    height: 720,
    mime_type: "image/jpeg",
    status,
    selectable,
    exclusion_reason: exclusionReason,
    already_used: status === "already_integrated",
  };
}

const catalog: EditorialFigureCatalogItem[] = [
  catalogItem(
    figureIds.a1,
    "Carte des flux réseau",
    sourceA,
    "Rapport public synthétique",
    "available",
    true,
  ),
  catalogItem(
    figureIds.a2,
    "Schéma de l’infrastructure",
    sourceA,
    "Rapport public synthétique",
    "available",
    true,
  ),
  catalogItem(
    figureIds.b1,
    "Photographie du serveur",
    sourceB,
    "Annexe technique synthétique",
    "available",
    true,
  ),
  catalogItem(
    figureIds.b2,
    "Capture de configuration",
    sourceB,
    "Annexe technique synthétique",
    "ineligible",
    false,
    "La licence de cette figure ne permet pas son intégration.",
  ),
  // A fourth *selectable* figure proves the BRIEF selection cap itself
  // disables the next checkbox, independently of licence exclusions.
  catalogItem(
    figureIds.a3,
    "Plan annoté des échanges",
    sourceA,
    "Rapport public synthétique",
    "available",
    true,
  ),
];

/* ------------------------------------------------------------------ */
/* Stub routes                                                         */
/* ------------------------------------------------------------------ */

function fulfillCatalog(route: Route, url: URL): Promise<void> {
  const status = url.searchParams.get("status");
  const source = url.searchParams.get("source");
  const query = url.searchParams.get("q")?.toLowerCase() ?? "";
  let items = catalog;
  if (status) items = items.filter((item) => item.status === status);
  if (source) {
    items = items.filter((item) => item.source.source_id === source);
  }
  if (query) {
    items = items.filter((item) =>
      [item.original_name, item.original_caption, item.context_before]
        .filter((value): value is string => Boolean(value))
        .some((value) => value.toLowerCase().includes(query)),
    );
  }
  return route.fulfill({
    json: {
      items,
      sources: [catalog[0]?.source, catalog[2]?.source].filter(
        (item): item is NonNullable<typeof item> => item !== undefined,
      ),
      total: items.length,
      available_count: items.filter((item) => item.selectable).length,
      offset: Number(url.searchParams.get("offset") ?? 0),
      limit: Number(url.searchParams.get("limit") ?? 30),
    },
  });
}

function historyItems(): EditorialHistoryItem[] {
  return [
    {
      proposal_id: paragraphProposalId,
      subject_id: subjectId,
      occurrence_id: paragraphId,
      state: "accepted",
      origin: "manual_patch",
      operation: "rewrite_paragraph",
      operation_label: "Réécriture",
      actor_id: "operator-synthetic",
      actor_display: "Opérateur synthétique · atelier",
      created_at: "2026-11-22T09:30:00Z",
      updated_at: "2026-11-22T09:30:01Z",
      canonical_result_artifact_id: artifactId,
      canonical_result_version: 4,
      canonical_result_stage: "synthesis",
      thread_id: null,
      thread_url: null,
    },
    {
      proposal_id: chartProposalId,
      subject_id: subjectId,
      occurrence_id: chartBlockId,
      state: "rejected",
      origin: "bridge_draft",
      operation: "rewrite_chart",
      operation_label: "Révision de graphique",
      actor_id: "operator-synthetic",
      actor_display: "Opérateur synthétique · revue",
      created_at: "2026-11-22T09:10:00Z",
      updated_at: "2026-11-22T09:12:00Z",
      canonical_result_artifact_id: null,
      canonical_result_version: null,
      canonical_result_stage: null,
      thread_id: threadId,
      thread_url: `/api/subjects/${subjectId}/editor/threads/${chartBlockId}`,
    },
  ];
}

async function installSyntheticRoutes(
  page: Page,
  config: ScenarioConfig,
  figureBytes: Buffer,
): Promise<ScenarioRuntime> {
  const runtime: ScenarioRuntime = { unexpectedRequests: [] };
  const snapshot = makeSnapshot(config);
  const subjectPath = `/api/subjects/${subjectId}`;
  const snapshotPath = `${subjectPath}/editor/snapshot`;
  const figureCatalogPath = `${subjectPath}/editor/figures`;

  await page.route("**/*", async (route: Route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== EDITORIAL_APP_ORIGIN) {
      runtime.unexpectedRequests.push(`${method} ${url.origin}${url.pathname}`);
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
    if (method === "GET" && url.pathname === subjectPath) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
      return;
    }
    if (method === "GET" && url.pathname === `${subjectPath}/content`) {
      await route.fulfill({ status: 404, body: "" });
      return;
    }
    if (method === "GET" && url.pathname === snapshotPath) {
      await route.fulfill({ json: snapshot });
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
    if (method === "GET" && url.pathname === `${subjectPath}/editor/history`) {
      await route.fulfill({
        json: { items: historyItems(), next_cursor: null, limit: 20 },
      });
      return;
    }
    if (method === "GET" && url.pathname === figureCatalogPath) {
      await fulfillCatalog(route, url);
      return;
    }
    if (
      method === "GET" &&
      url.pathname.startsWith(`${figureCatalogPath}/`) &&
      url.pathname.endsWith("/thumbnail")
    ) {
      await route.fulfill({
        status: 200,
        contentType: "image/jpeg",
        body: figureBytes,
      });
      return;
    }
    const previewMatch = url.pathname.match(
      new RegExp(`^${figureCatalogPath}/([^/]+)/preview$`),
    );
    if (method === "GET" && previewMatch) {
      const item = catalog.find(
        (candidate) => candidate.figure_id === previewMatch[1],
      );
      if (!item) {
        await route.fulfill({
          status: 404,
          json: { detail: { code: "editorial_figure_not_found" } },
        });
        return;
      }
      await route.fulfill({
        json: {
          ...item,
          preview_image_url: `${figureCatalogPath}/${item.figure_id}/preview/image`,
        },
      });
      return;
    }
    if (
      method === "GET" &&
      url.pathname.startsWith(`${figureCatalogPath}/`) &&
      url.pathname.endsWith("/preview/image")
    ) {
      await route.fulfill({
        status: 200,
        contentType: "image/jpeg",
        body: figureBytes,
      });
      return;
    }
    const proposalMatch = url.pathname.match(
      /^\/api\/subjects\/[^/]+\/editor\/threads\/([^/]+)\/proposals\/([^/]+)$/,
    );
    if (method === "GET" && proposalMatch) {
      const proposalId = decodeURIComponent(proposalMatch[2] ?? "");
      const proposal =
        proposalId === chartProposalId
          ? chartProposal()
          : proposalId === paragraphProposalId
            ? paragraphProposal()
            : null;
      if (!proposal) {
        await route.fulfill({
          status: 404,
          json: { detail: { code: "proposal_not_found" } },
        });
        return;
      }
      await route.fulfill({ json: proposal });
      return;
    }
    const acceptMatch = url.pathname.match(
      /^\/api\/subjects\/[^/]+\/editor\/threads\/([^/]+)\/proposals\/([^/]+)\/accept$/,
    );
    if (method === "POST" && acceptMatch) {
      const proposalId = decodeURIComponent(acceptMatch[2] ?? "");
      if (
        config.paragraphThread === "conflict" &&
        proposalId === paragraphProposalId
      ) {
        await route.fulfill({
          status: 409,
          json: {
            detail: {
              code: "proposal_base_stale",
              message: conflictMessage,
            },
          },
        });
        return;
      }
      await route.fulfill({
        json: {
          id: proposalId,
          state: "accepted",
          state_version: 2,
          artifact_id: artifactId,
          artifact_version: 5,
          snapshot_id: snapshotId,
          impact_id: "91000000-0000-4000-8000-000000000001",
          impact_state: "pending",
        },
      });
      return;
    }
    const threadMatch = url.pathname.match(
      /^\/api\/subjects\/[^/]+\/editor\/threads\/([^/]+)$/,
    );
    if (method === "GET" && threadMatch) {
      const occurrenceId = decodeURIComponent(threadMatch[1] ?? "");
      const thread = threadFor(occurrenceId, config);
      const etag = thread.etag;
      if (route.request().headers()["if-none-match"] === etag) {
        await route.fulfill({ status: 304, headers: { ETag: etag } });
        return;
      }
      const afterSequence = Number(
        url.searchParams.get("after_sequence") ?? "0",
      );
      await route.fulfill({
        status: 200,
        headers: { ETag: etag },
        json: {
          ...thread,
          timeline: thread.timeline.filter(
            (event) => event.sequence > afterSequence,
          ),
        },
      });
      return;
    }
    if (
      method === "GET" &&
      url.pathname ===
        `${subjectPath}/production/artifacts/publication/${historicalArtifactId}`
    ) {
      await route.fulfill({
        json: {
          artifact_id: historicalArtifactId,
          production_run_id: runId,
          subject_id: subjectId,
          stage: "publication",
          version: 3,
          input_hash: "f".repeat(64),
          canonical_sha256: canonicalSha,
          status: "verified",
          metadata: {},
          publication_kind: "article",
          read_only_reason: "historical_version",
          canonical_content: {
            schema_version: "4",
            subject_id: subjectId,
            publication_language: "fr",
            title: "Publication historique synthétique",
            lead: [
              {
                text: "Chapeau historique conservé pour la consultation.",
                evidence_refs: [
                  {
                    source_document_id: "source-synthetic-1",
                    kind: "fact",
                    evidence_key: "synthetic-fact-1",
                  },
                ],
              },
            ],
            sections: [
              {
                kind: "contexte",
                heading: "Contexte historique",
                paragraphs: [
                  {
                    text: "Ce paragraphe historique reste affiché en lecture seule, sans possibilité de modification canonique.",
                    evidence_refs: [
                      {
                        source_document_id: "source-synthetic-1",
                        kind: "fact",
                        evidence_key: "synthetic-fact-1",
                      },
                    ],
                  },
                ],
              },
            ],
            timeline: [],
            indicators: [],
            sources: [
              {
                source_document_id: "source-synthetic-1",
                canonical_url: "https://publisher.example/synthetic-source",
                title: "Rapport public synthétique",
                publisher: "Éditeur synthétique",
                published_at: "2026-11-01T00:00:00Z",
                tier: "core",
                kind: "publication",
                role: "primary",
              },
            ],
            uncertainties: [],
            tables: [],
            diagrams: [],
            figures: [],
          },
        },
      });
      return;
    }

    runtime.unexpectedRequests.push(`${method} ${url.pathname}${url.search}`);
    await route.abort("blockedbyclient");
  });

  return runtime;
}

/* ------------------------------------------------------------------ */
/* Shared page checks                                                  */
/* ------------------------------------------------------------------ */

async function pageViolations(page: Page): Promise<string[]> {
  const serialized: unknown = await page.evaluate(`(() => {
    const violations = [];
    if (document.documentElement.scrollWidth > window.innerWidth + 1) {
      violations.push(
        "débordement horizontal: " +
          document.documentElement.scrollWidth +
          " > " +
          window.innerWidth,
      );
    }
    const visible = (element) => {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return (
        style.visibility !== "hidden" &&
        style.display !== "none" &&
        rect.width > 1 &&
        rect.height > 1
      );
    };
    const textOf = (element) =>
      (element.innerText || element.textContent || "")
        .replace(/\\s+/g, " ")
        .trim();
    const accessibleName = (element) => {
      const ariaLabel = element.getAttribute("aria-label");
      if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
      const labelledBy = element.getAttribute("aria-labelledby");
      if (labelledBy) {
        const name = labelledBy
          .split(/\\s+/)
          .map((id) => document.getElementById(id))
          .filter((item) => item !== null)
          .map((item) => textOf(item))
          .join(" ")
          .trim();
        if (name) return name;
      }
      if (
        element instanceof HTMLInputElement ||
        element instanceof HTMLTextAreaElement ||
        element instanceof HTMLSelectElement
      ) {
        const labels = element.labels;
        if (labels && labels.length > 0) {
          const name = Array.from(labels)
            .map((label) => textOf(label))
            .join(" ")
            .trim();
          if (name) return name;
        }
      }
      const content = textOf(element);
      if (content) return content;
      const title = element.getAttribute("title");
      return title && title.trim() ? title.trim() : "";
    };
    const interactive = Array.from(
      document.querySelectorAll(
        "button, a[href], input:not([type='hidden']), select, textarea, summary",
      ),
    ).filter(visible);
    for (const element of interactive) {
      if (!accessibleName(element)) {
        violations.push(
          "contrôle sans nom accessible: " +
            element.tagName.toLowerCase() +
            "." +
            (element.className || "(sans classe)"),
        );
      }
      const rect = element.getBoundingClientRect();
      if (rect.left < -1 || rect.right > window.innerWidth + 1) {
        violations.push(
          "contrôle hors viewport horizontalement: " +
            element.tagName.toLowerCase() +
            "." +
            (element.className || "(sans classe)") +
            " (" +
            Math.round(rect.left) +
            "→" +
            Math.round(rect.right) +
            ")",
        );
      }
    }
    const documentColumn = document.querySelector(".editorial-document-column");
    if (documentColumn) {
      const columnRect = documentColumn.getBoundingClientRect();
      for (const panel of Array.from(
        document.querySelectorAll(".editorial-visual-insertion__panel"),
      )) {
        if (!visible(panel)) continue;
        const rect = panel.getBoundingClientRect();
        if (rect.left < columnRect.left - 1 || rect.right > columnRect.right + 1) {
          violations.push(
            "panneau d’insertion hors colonne document: " +
              Math.round(rect.left) +
              "→" +
              Math.round(rect.right) +
              " hors " +
              Math.round(columnRect.left) +
              "→" +
              Math.round(columnRect.right),
          );
        }
      }
    }
    const groups = [
      ".editorial-topbar__metadata",
      ".editorial-block-actions",
      ".editorial-inspector-tabs",
      ".editorial-conversation__suggestions",
    ];
    for (const selector of groups) {
      const containers = Array.from(
        document.querySelectorAll(selector),
      ).filter(visible);
      for (const container of containers) {
        const children = Array.from(
          container.querySelectorAll(
            ":scope > button, :scope > a, :scope > input, :scope > select, :scope > .button, :scope > .editorial-topbar__menu-wrap",
          ),
        ).filter(visible);
        const rects = children.map((element) => {
          const rect = element.getBoundingClientRect();
          return {
            label:
              element.tagName.toLowerCase() +
              "." +
              (element.className || "(sans classe)"),
            left: rect.left,
            top: rect.top,
            right: rect.right,
            bottom: rect.bottom,
          };
        });
        for (let first = 0; first < rects.length; first += 1) {
          for (let second = first + 1; second < rects.length; second += 1) {
            const a = rects[first];
            const b = rects[second];
            if (!a || !b) continue;
            const overlapWidth =
              Math.min(a.right, b.right) - Math.max(a.left, b.left);
            const overlapHeight =
              Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
            if (overlapWidth > 2 && overlapHeight > 2) {
              violations.push(
                "chevauchement de contrôles (" +
                  selector +
                  "): " +
                  a.label +
                  " / " +
                  b.label,
              );
            }
          }
        }
      }
    }
    for (const bar of Array.from(
      document.querySelectorAll(".editorial-block-actions--block"),
    ).filter(visible)) {
      const block = bar.closest(".editorial-document__block");
      const nextBlock = block ? block.nextElementSibling : null;
      if (!block || !nextBlock) continue;
      const blockRect = block.getBoundingClientRect();
      const barRect = bar.getBoundingClientRect();
      const nextRect = nextBlock.getBoundingClientRect();
      if (
        barRect.bottom > blockRect.bottom + 1 ||
        barRect.left < blockRect.left - 1 ||
        barRect.right > blockRect.right + 1
      ) {
        violations.push(
          "barre d’actions hors de son bloc: " +
            Math.round(barRect.left) +
            "→" +
            Math.round(barRect.right) +
            " / " +
            Math.round(barRect.top) +
            "→" +
            Math.round(barRect.bottom),
        );
      }
      const overlapWidth =
        Math.min(barRect.right, nextRect.right) -
        Math.max(barRect.left, nextRect.left);
      const overlapHeight =
        Math.min(barRect.bottom, nextRect.bottom) -
        Math.max(barRect.top, nextRect.top);
      if (overlapWidth > 1 && overlapHeight > 1) {
        violations.push(
          "barre d’actions recouvre le bloc suivant: chevauchement " +
            Math.round(overlapWidth) +
            "×" +
            Math.round(overlapHeight),
        );
      }
    }
    const main = document.querySelector("main");
    const mainText = main ? main.innerText : "";
    if (/\\bundefined\\b|\\[object/i.test(mainText)) {
      violations.push("chaîne technique visible (undefined/[object])");
    }
    if (
      /\\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\\b/i.test(
        mainText,
      )
    ) {
      violations.push("UUID brut visible dans le document");
    }
    return JSON.stringify(violations);
  })()`);
  const parsed: unknown =
    typeof serialized === "string" ? (JSON.parse(serialized) as unknown) : [];
  if (!Array.isArray(parsed)) {
    throw new Error("Invalid page violation payload");
  }
  return parsed.filter((item): item is string => typeof item === "string");
}

/* ------------------------------------------------------------------ */
/* State definitions                                                   */
/* ------------------------------------------------------------------ */

const paragraphSelector = `.editorial-document__block[data-block-id="${paragraphId}"]`;
const secondParagraphSelector = `.editorial-document__block[data-block-id="${secondParagraphId}"]`;
const headingSelector = `.editorial-document__block[data-block-id="${headingId}"]`;
const leadSelector = `.editorial-document__block[data-block-id="${leadId}"]`;
const chartSelector = `.editorial-document__block[data-element-key="${CHART_KEY}"]`;
const figureASelector = `.editorial-document__block[data-element-key="${FIGURE_A_KEY}"]`;
const figureBSelector = `.editorial-document__block[data-element-key="${FIGURE_B_KEY}"]`;

async function selectParagraph(page: Page): Promise<void> {
  const paragraph = page.locator(paragraphSelector);
  await paragraph.click();
  await expect(paragraph).toHaveAttribute("data-selected", "true");
}

async function openFigureGallery(page: Page): Promise<Locator> {
  await selectParagraph(page);
  await page
    .locator(".editorial-block-actions")
    .getByRole("button", { name: "Ajouter après" })
    .click();
  await page.getByRole("menuitem", { name: "Image issue des sources" }).click();
  const gallery = page.getByRole("dialog", {
    name: "Galerie d’images sources",
  });
  await expect(gallery).toBeVisible();
  return gallery;
}

/**
 * Scrolls an independently scrollable panel (inspector, dialog body) so the
 * target starts at its top edge. Captures used by reviewers must start on a
 * clean block boundary, not in the middle of a wrapped row.
 */
async function alignInScrollContainer(
  container: Locator,
  target: Locator,
  offset = 8,
): Promise<void> {
  const handle = await target.elementHandle();
  if (!handle) throw new Error("Scroll alignment target is not rendered");
  await container.evaluate(
    (containerElement, payload) => {
      const scrollable = containerElement as unknown as {
        scrollTop: number;
        getBoundingClientRect(): { top: number };
      };
      const element = payload.target as unknown as {
        getBoundingClientRect(): { top: number };
      };
      scrollable.scrollTop +=
        element.getBoundingClientRect().top -
        scrollable.getBoundingClientRect().top -
        payload.offset;
    },
    { target: handle, offset },
  );
}

function inspectorColumn(page: Page): Locator {
  return page.locator(".editorial-workspace__inspector-column");
}

/** Centers a control in the window viewport, away from sticky headers. */
async function centerInViewport(target: Locator): Promise<void> {
  await target.evaluate((element) => {
    const node = element as unknown as {
      scrollIntoView(options?: { block?: string; inline?: string }): void;
    };
    node.scrollIntoView({ block: "center", inline: "nearest" });
  });
}

interface SecondaryCapture {
  fileName: string;
  prepare: (page: Page) => Promise<void>;
  expect: (page: Page) => Promise<void>;
}

interface MatrixState {
  id: string;
  name: string;
  scenario: ScenarioConfig;
  prepare: (page: Page) => Promise<void>;
  expect: (page: Page) => Promise<void>;
  secondary?: SecondaryCapture;
}

const STATES: MatrixState[] = [
  {
    id: "01",
    name: "document-initial",
    scenario: {},
    prepare: () => Promise.resolve(),
    expect: async (page) => {
      await expect(page.locator(".editorial-workspace__grid")).toBeVisible();
      await expect(
        page
          .locator(".editorial-document")
          .getByRole("heading", { name: "Bulletin synthétique M23" }),
      ).toBeVisible();
      await expect(page.locator(paragraphSelector)).toBeVisible();
      await expect(page.locator(chartSelector)).toBeVisible();
    },
  },
  {
    id: "02",
    name: "block-selected",
    scenario: {},
    prepare: selectParagraph,
    expect: async (page) => {
      await expect(
        page
          .locator(".editorial-block-actions")
          .getByRole("button", { name: "Modifier", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("toolbar", { name: /Actions du bloc : Paragraphe/ }),
      ).toBeVisible();
    },
  },
  {
    id: "03",
    name: "inline-edit",
    scenario: {},
    prepare: async (page) => {
      await selectParagraph(page);
      await page
        .locator(".editorial-block-actions")
        .getByRole("button", { name: "Modifier", exact: true })
        .click();
      await expect(
        page.getByRole("textbox", { name: "Texte du bloc" }),
      ).toBeVisible();
    },
    expect: async (page) => {
      await expect(
        page.getByRole("button", { name: "Enregistrer", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Annuler", exact: true }),
      ).toBeVisible();
    },
  },
  {
    id: "04",
    name: "local-draft",
    scenario: {},
    prepare: async (page) => {
      await selectParagraph(page);
      await page
        .locator(".editorial-block-actions")
        .getByRole("button", { name: "Modifier", exact: true })
        .click();
      await page
        .getByRole("textbox", { name: "Texte du bloc" })
        .fill(
          "Brouillon local conservé pour ce bloc synthétique, non enregistré comme version canonique.",
        );
      // A hovered visual's floating action bar sits over the next block's top
      // edge; parking the pointer avoids intercepting this target selection.
      await page.mouse.move(2, 2);
      await page.locator(secondParagraphSelector).click();
      await expect(page.locator(paragraphSelector)).toContainText(
        "Brouillon local",
      );
      await expect(
        page.getByRole("button", { name: "Reprendre le brouillon" }).first(),
      ).toBeVisible();
    },
    expect: async (page) => {
      await page.locator(paragraphSelector).scrollIntoViewIfNeeded();
      await expect(
        page
          .locator(".editorial-outline")
          .getByText("Brouillon local", { exact: true })
          .first(),
      ).toBeVisible();
    },
  },
  {
    id: "05",
    name: "bridge-discussion",
    scenario: { paragraphThread: "discussion" },
    prepare: async (page) => {
      await selectParagraph(page);
      await expect(
        page.getByRole("textbox", { name: "Votre message à Bridge" }),
      ).toBeVisible();
      await alignInScrollContainer(
        inspectorColumn(page),
        page.locator(".editorial-conversation__composer"),
        8,
      );
    },
    expect: async (page) => {
      await expect(
        page.getByRole("group", { name: "Suggestions de message" }),
      ).toBeVisible();
      await expect(
        page.getByText("Discussion sur :", { exact: false }).first(),
      ).toBeVisible();
      await expect(
        page.getByText(
          "Une réponse est disponible dans ce fil. Consultez-la avant toute décision.",
        ),
      ).toBeVisible();
    },
  },
  {
    id: "06",
    name: "proposal-diff",
    scenario: { paragraphThread: "proposal" },
    prepare: async (page) => {
      await selectParagraph(page);
      const card = page
        .getByRole("article", { name: "Proposition éditoriale" })
        .first();
      await expect(card).toBeVisible();
      await card.getByRole("button", { name: "Différences" }).click();
      await expect(
        card.getByRole("group", { name: "Différences du paragraphe" }),
      ).toBeVisible();
      await alignInScrollContainer(inspectorColumn(page), card, 8);
    },
    expect: async (page) => {
      const card = page
        .getByRole("article", { name: "Proposition éditoriale" })
        .first();
      for (const action of ["Accepter", "Rejeter", "Demander une variante"]) {
        await expect(
          card.getByRole("button", { name: action, exact: true }),
        ).toBeVisible();
      }
      await expect(
        card.getByText("Ajouté ·", { exact: false }).first(),
      ).toBeVisible();
    },
  },
  {
    id: "07",
    name: "chart-selected",
    scenario: {},
    prepare: async (page) => {
      await page.locator(chartSelector).click();
      await expect(page.locator(chartSelector)).toHaveAttribute(
        "data-selected",
        "true",
      );
      await expect(
        page.getByRole("toolbar", {
          name: "Actions du visuel : Graphique des volumes observés",
        }),
      ).toBeVisible();
      // Scrolling recomputes hover; park the pointer outside the document so
      // the selected block's action bar stays rendered.
      await page.mouse.move(2, 2);
      await centerInViewport(
        page.getByRole("toolbar", {
          name: "Actions du visuel : Graphique des volumes observés",
        }),
      );
      await expect(
        page.getByRole("toolbar", {
          name: "Actions du visuel : Graphique des volumes observés",
        }),
      ).toBeInViewport({ ratio: 0.5 });
    },
    expect: async (page) => {
      await expect(
        page.getByRole("tablist", { name: "Panneaux de l’inspecteur" }),
      ).toBeVisible();
      await expect(
        page.getByRole("tab", { name: "Discussion Bridge" }),
      ).toHaveAttribute("aria-selected", "true");
    },
  },
  {
    id: "08",
    name: "chart-correction-proposal",
    scenario: { chartProposal: true },
    prepare: async (page) => {
      await page.locator(chartSelector).click();
      await page.getByRole("button", { name: "Demander à Bridge" }).click();
      const card = page
        .getByRole("article", { name: "Proposition éditoriale" })
        .first();
      await expect(card).toBeVisible();
      await card.getByText("Preuves utilisées").click();
      // The INDEX requires the field comparison rows (title, caption, unit,
      // axes); align on them rather than on the point values lower in the card.
      await alignInScrollContainer(
        inspectorColumn(page),
        card.locator(".visual-proposal-diff__fields"),
        8,
      );
      await expect(
        card.locator(".visual-proposal-diff__fields"),
      ).toBeInViewport({ ratio: 0.5 });
    },
    expect: async (page) => {
      const card = page
        .getByRole("article", { name: "Proposition éditoriale" })
        .first();
      await expect(
        card.locator(".visual-proposal-diff__current").filter({
          hasText: "Graphique des volumes observés",
        }),
      ).toBeVisible();
      await expect(
        card.locator(".visual-proposal-diff__proposed").filter({
          hasText: "Volumes corrigés après revue",
        }),
      ).toBeVisible();
      await expect(
        card.getByText("Valeur actuelle ·", { exact: false }).first(),
      ).toBeVisible();
      await expect(
        card.getByText("Valeur proposée ·", { exact: false }).first(),
      ).toBeVisible();
      await expect(card.locator(".visual-proposal-diff__field")).toHaveCount(5);
      await expect(
        card.locator(".visual-proposal-diff__field[data-changed='true']"),
      ).toHaveCount(2);
      await expect(
        card
          .locator(".visual-proposal-diff__evidence code")
          .filter({ hasText: "synthetic-fact-1" }),
      ).toBeVisible();
    },
  },
  {
    id: "09",
    name: "gallery-multi-selection",
    scenario: {},
    prepare: async (page) => {
      const gallery = await openFigureGallery(page);
      await gallery
        .getByRole("checkbox", { name: "Sélectionner Carte des flux réseau" })
        .check();
      await gallery
        .getByRole("checkbox", {
          name: "Sélectionner Schéma de l’infrastructure",
        })
        .check();
      await expect(gallery.getByText("2 images sélectionnées")).toBeVisible();
    },
    expect: async (page) => {
      const gallery = page.getByRole("dialog", {
        name: "Galerie d’images sources",
      });
      await expect(
        gallery.getByText("Sélectionnée", { exact: true }).first(),
      ).toBeVisible();
      await expect(
        gallery.getByRole("checkbox", {
          name: "Sélectionner Carte des flux réseau",
        }),
      ).toBeChecked();
    },
    secondary: {
      fileName: "09b-selection-panel",
      prepare: async (page) => {
        await page
          .getByRole("button", { name: "Fermer la galerie d’images sources" })
          .click();
        const panel = page.locator(".gallery-selection");
        await expect(panel).toBeVisible();
        // Put the whole insertion panel just below the top bar, then scroll
        // its own content to the actions; the sticky header stays visible.
        const insertion = page.locator(".editorial-figure-insertion");
        await expect(insertion).toBeVisible();
        await page.evaluate(`(() => {
          const element = document.querySelector('.editorial-figure-insertion');
          if (!element) return;
          const topbar = parseFloat(
            getComputedStyle(document.documentElement).getPropertyValue(
              '--editorial-topbar-height',
            ),
          ) || 84;
          const rect = element.getBoundingClientRect();
          window.scrollBy(0, rect.top - topbar - 8);
        })()`);
        await insertion.evaluate((element) => {
          const node = element as unknown as {
            scrollTop: number;
            scrollHeight: number;
          };
          node.scrollTop = node.scrollHeight;
        });
        await expect(
          insertion.getByRole("heading", {
            name: "Intégrer des images sources",
          }),
        ).toBeInViewport();
        await expect(
          insertion.getByRole("button", {
            name: "Intégrer les images sélectionnées",
          }),
        ).toBeInViewport();
      },
      expect: async (page) => {
        const panel = page.locator(".gallery-selection");
        await expect(panel.getByText("2 images sélectionnées")).toBeVisible();
        await expect(
          panel.getByRole("button", {
            name: "Intégrer les images sélectionnées",
          }),
        ).toBeVisible();
        await expect(
          panel.getByText("Emplacement ·", { exact: false }).first(),
        ).toBeVisible();
      },
    },
  },
  {
    id: "10",
    name: "replace-image-comparison",
    scenario: {},
    prepare: async (page) => {
      await page.locator(figureASelector).click();
      await page
        .locator(".editorial-block-actions")
        .getByRole("button", { name: "Remplacer", exact: true })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Remplacer l’image intégrée",
      });
      await expect(dialog).toBeVisible();
      await dialog
        .getByRole("radio", { name: "Choisir Carte des flux réseau" })
        .check();
      const comparison = dialog.getByRole("region", {
        name: "Comparaison des images",
      });
      await expect(comparison).toContainText(
        "Légende archivée de Carte des flux réseau",
      );
      await alignInScrollContainer(
        dialog.locator(".editorial-gallery-replace__layout"),
        comparison,
        8,
      );
    },
    expect: async (page) => {
      const dialog = page.getByRole("dialog", {
        name: "Remplacer l’image intégrée",
      });
      await expect(
        dialog.getByRole("heading", { name: "Remplacer l’image intégrée" }),
      ).toBeInViewport();
      await expect(
        dialog.getByRole("button", {
          name: "Fermer le remplacement d’image",
        }),
      ).toBeInViewport();
      await expect(
        dialog.getByRole("heading", { name: "Image actuelle" }),
      ).toBeVisible();
      await expect(
        dialog.getByRole("heading", { name: "Nouvelle image" }),
      ).toBeVisible();
      await expect(
        dialog.getByText("L’archive d’origine n’est pas modifiée", {
          exact: false,
        }),
      ).toBeVisible();
    },
  },
  {
    id: "11",
    name: "move-mode-banner",
    scenario: {},
    prepare: async (page) => {
      await page.locator(figureASelector).click();
      await page
        .locator(".editorial-block-actions")
        .getByRole("button", { name: "Déplacer", exact: true })
        .click();
      const banner = page.getByRole("region", {
        name: "Déplacement du visuel",
      });
      await expect(banner).toBeVisible();
      await banner.scrollIntoViewIfNeeded();
    },
    expect: async (page) => {
      await expect(
        page.getByRole("heading", { name: "Choisir un nouvel emplacement" }),
      ).toBeVisible();
      await expect(
        page.getByRole("region", { name: "Déplacement du visuel" }),
      ).toBeInViewport();
      await expect(
        page.getByText("Cliquez un paragraphe", { exact: false }),
      ).toBeVisible();
    },
  },
  {
    id: "12",
    name: "move-provisional-preview",
    scenario: {},
    prepare: async (page) => {
      await page.locator(figureASelector).click();
      await page
        .locator(".editorial-block-actions")
        .getByRole("button", { name: "Déplacer", exact: true })
        .click();
      await page.locator(secondParagraphSelector).click();
      const preview = page.getByRole("region", {
        name: "Aperçu provisoire de l’emplacement",
      });
      await expect(preview).toBeVisible();
      await expect(preview).toContainText("Insertion prévue : après «");
      await preview.scrollIntoViewIfNeeded();
      // The sticky move banner must remain visible alongside the preview.
      await expect(
        page.getByRole("region", { name: "Déplacement du visuel" }),
      ).toBeInViewport();
      await expect(preview).toBeInViewport();
    },
    expect: async (page) => {
      await expect(
        page.getByText("Aperçu non canonique", { exact: false }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Enregistrer l’emplacement" }),
      ).toBeVisible();
    },
  },
  {
    id: "13",
    name: "reorder-panel",
    scenario: {},
    prepare: async (page) => {
      await page.locator(figureASelector).click();
      await page
        .locator(".editorial-block-actions")
        .getByRole("button", { name: "Réorganiser", exact: true })
        .click();
      const panel = page.getByRole("group", {
        name: "Réorganiser les visuels",
      });
      await expect(panel).toBeVisible();
      await panel.scrollIntoViewIfNeeded();
    },
    expect: async (page) => {
      const panel = page.getByRole("group", {
        name: "Réorganiser les visuels",
      });
      await expect(panel.getByText(/Ordre prévu :/)).toContainText(
        "1. Graphique des volumes observés",
      );
      await expect(
        panel.getByRole("button", { name: "Enregistrer l’ordre" }),
      ).toBeVisible();
    },
  },
  {
    id: "14",
    name: "version-conflict",
    scenario: { paragraphThread: "conflict" },
    prepare: async (page) => {
      await selectParagraph(page);
      const card = page
        .getByRole("article", { name: "Proposition éditoriale" })
        .first();
      await expect(card).toBeVisible();
      await card.getByRole("button", { name: "Accepter", exact: true }).click();
      await expect(
        page.getByText(conflictMessage, { exact: true }),
      ).toBeVisible();
      // The INDEX requires the disabled decision buttons *and* the conflict
      // message; align on the actions so both fit in the capture.
      await alignInScrollContainer(
        inspectorColumn(page),
        card.locator(".editorial-proposal-card__actions"),
        8,
      );
    },
    expect: async (page) => {
      const card = page
        .getByRole("article", { name: "Proposition éditoriale" })
        .first();
      await expect(
        card.getByRole("button", { name: "Accepter", exact: true }),
      ).toBeInViewport();
      await expect(
        card.getByRole("button", { name: "Rejeter", exact: true }),
      ).toBeInViewport();
      await expect(
        page.getByText(conflictMessage, { exact: true }),
      ).toBeInViewport();
      await expect(
        card.getByRole("button", { name: "Comparer à nouveau" }),
      ).toBeVisible();
      await expect(
        card.getByRole("button", { name: "Accepter", exact: true }),
      ).toHaveAttribute("aria-disabled", "true");
      await expect(
        card.getByRole("button", { name: "Rejeter", exact: true }),
      ).toBeDisabled();
    },
  },
  {
    id: "15",
    name: "pdf-obsolete",
    scenario: { pdfStale: true },
    prepare: () => Promise.resolve(),
    expect: async (page) => {
      await expect(page.locator(".editorial-status")).toHaveText(
        "PDF obsolète",
      );
      await expect(
        page.getByRole("button", { name: "Régénérer le PDF", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Voir le PDF", exact: true }),
      ).toBeDisabled();
    },
  },
  {
    id: "16",
    name: "historical-read-only",
    scenario: { historical: true },
    prepare: async (page) => {
      await expect(
        page.getByRole("heading", {
          name: "Atelier éditorial — version 3",
        }),
      ).toBeVisible();
    },
    expect: async (page) => {
      await expect(
        page.getByText(
          "Ce paragraphe historique reste affiché en lecture seule",
          {
            exact: false,
          },
        ),
      ).toBeVisible();
      await expect(
        page.getByRole("link", { name: "Revenir à la version courante" }),
      ).toBeVisible();
    },
  },
  {
    id: "17",
    name: "brief-figure-limit",
    scenario: { kind: "brief" },
    prepare: async (page) => {
      const gallery = await openFigureGallery(page);
      for (const name of [
        "Carte des flux réseau",
        "Schéma de l’infrastructure",
        "Photographie du serveur",
      ]) {
        await gallery
          .getByRole("checkbox", { name: `Sélectionner ${name}` })
          .check();
      }
      await expect(gallery.getByText("3 images sélectionnées")).toBeVisible();
      const limitNote = gallery.getByText(
        /Sélection : 3\/3 — le plafond de sélection est atteint/,
      );
      await expect(limitNote).toBeVisible();
      const cappedCheckbox = gallery.getByRole("checkbox", {
        name: "Sélectionner Plan annoté des échanges",
      });
      await expect(cappedCheckbox).toBeDisabled();
      const describedBy = await cappedCheckbox.getAttribute("aria-describedby");
      const limitId = await limitNote.getAttribute("id");
      expect(limitId).toBeTruthy();
      expect(describedBy?.split(" ")).toContain(limitId);
      await limitNote.scrollIntoViewIfNeeded();
    },
    expect: async (page) => {
      const gallery = page.getByRole("dialog", {
        name: "Galerie d’images sources",
      });
      await expect(
        gallery.getByRole("checkbox", {
          name: "Sélectionner Capture de configuration",
        }),
      ).toBeDisabled();
      await expect(
        gallery.getByText(/Capacité du document : 0\/3 figures intégrées/),
      ).toBeVisible();
      await expect(
        gallery.getByText(
          /Sélection : 3\/3 — le plafond de sélection est atteint/,
        ),
      ).toBeInViewport();
    },
  },
  {
    id: "18",
    name: "visuals-tab",
    scenario: {},
    prepare: async (page) => {
      await page.locator(chartSelector).click();
      await page.getByRole("tab", { name: "Visuels" }).click();
      await expect(
        page.getByRole("list", { name: "Éléments visuels du document" }),
      ).toBeVisible();
      // Bring the archive-gallery entry above the inspector fold so the
      // capture actually shows it.
      await page
        .getByRole("button", { name: "Ouvrir la galerie des images archivées" })
        .scrollIntoViewIfNeeded();
    },
    expect: async (page) => {
      const list = page.getByRole("list", {
        name: "Éléments visuels du document",
      });
      await expect(list.getByText("Graphique", { exact: true })).toBeVisible();
      await expect(
        list.getByText("Figure", { exact: true }).first(),
      ).toBeVisible();
      await expect(
        page.getByRole("button", {
          name: "Ouvrir la galerie des images archivées",
        }),
      ).toBeInViewport();
    },
  },
  {
    id: "19",
    name: "history-panel",
    scenario: {},
    prepare: async (page) => {
      await page.getByRole("button", { name: "Historique et détails" }).click();
      await page
        .getByRole("button", { name: "Historique", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "Historique des propositions" }),
      ).toBeVisible();
    },
    expect: async (page) => {
      const history = page.locator(".editorial-history");
      await expect(history.getByText("Réécriture")).toBeVisible();
      await expect(history.getByText("Révision de graphique")).toBeVisible();
      await expect(
        history.getByRole("button", { name: "Fermer l’historique" }),
      ).toBeVisible();
    },
  },
];

/* ------------------------------------------------------------------ */
/* Matrix test                                                         */
/* ------------------------------------------------------------------ */

/**
 * Measure an element only once its box is identical on two consecutive
 * animation frames. Webfonts and archived figure thumbnails can settle after
 * the first paint, so measuring immediately after `toBeVisible()` made the
 * F6-C overlap assertions flaky under load. The assertions themselves are
 * unchanged; only the measurement waits for a stable layout.
 */
async function stableBoundingBox(
  locator: Locator,
): Promise<{ x: number; y: number; width: number; height: number } | null> {
  const signature = (box: {
    x: number;
    y: number;
    width: number;
    height: number;
  }) => `${box.x},${box.y},${box.width},${box.height}`;
  await expect
    .poll(
      async () => {
        const first = await locator.boundingBox();
        if (first === null) return "missing";
        await locator.page().waitForTimeout(50);
        const second = await locator.boundingBox();
        if (second === null) return "missing";
        return signature(first) === signature(second) ? "stable" : "moving";
      },
      { timeout: 5_000 },
    )
    .toBe("stable");
  return locator.boundingBox();
}

test("F6-A — matrice de captures UX desktop (19 états × 4 largeurs)", async ({
  page,
}) => {
  test.setTimeout(900_000);
  const context = page.context();
  const figureBytes = await readFile(
    new URL("./fixtures/figure-gallery-thumbnail.jpg", import.meta.url),
  );
  const failures: string[] = [];
  const captured: string[] = [];
  // Debug helper: UX_MATRIX_ONLY=01,08 / UX_MATRIX_WIDTH=1024 narrows a run.
  const onlyIds = (process.env.UX_MATRIX_ONLY ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  const onlyWidth = Number(process.env.UX_MATRIX_WIDTH ?? "");
  const states =
    onlyIds.length > 0
      ? STATES.filter((state) => onlyIds.includes(state.id))
      : STATES;
  const viewports =
    Number.isFinite(onlyWidth) && onlyWidth > 0
      ? VIEWPORTS.filter((viewport) => viewport.width === onlyWidth)
      : VIEWPORTS;
  const expectedPrimary = states.length * viewports.length;
  const expectedSecondary =
    states.filter((state) => state.secondary !== undefined).length *
    viewports.length;

  // First Vite request compiles the editor route (and may re-optimise its
  // dependencies), so warm the app once instead of letting the first capture
  // absorb that cold start.
  const warmupPage = await context.newPage();
  try {
    await installSyntheticRoutes(warmupPage, {}, figureBytes);
    await warmupPage.setViewportSize({ width: 1024, height: 800 });
    await warmupPage.goto(`/subjects/${subjectId}/editor`);
    await expect(warmupPage.locator(".editorial-workspace")).toBeVisible({
      timeout: 60_000,
    });
  } finally {
    await warmupPage.close();
  }

  for (const viewport of viewports) {
    const directory = resolve(matrixRoot, String(viewport.width));
    await mkdir(directory, { recursive: true });
    for (const state of states) {
      const scenarioPage = await context.newPage();
      scenarioPage.setDefaultTimeout(8_000);
      const label = `${state.id}-${state.name}@${viewport.width}x${viewport.height}`;
      try {
        const runtime = await installSyntheticRoutes(
          scenarioPage,
          state.scenario,
          figureBytes,
        );
        await scenarioPage.setViewportSize({
          width: viewport.width,
          height: viewport.height,
        });
        const query = state.scenario.historical
          ? `?publication_artifact_id=${historicalArtifactId}`
          : "";
        await scenarioPage.goto(`/subjects/${subjectId}/editor${query}`);
        if (state.scenario.historical) {
          await expect(
            scenarioPage.getByRole("heading", {
              name: "Atelier éditorial — version 3",
            }),
          ).toBeVisible({ timeout: 20_000 });
        } else {
          await expect(
            scenarioPage.locator(".editorial-workspace"),
          ).toBeVisible({ timeout: 20_000 });
        }

        await state.prepare(scenarioPage);
        await state.expect(scenarioPage);

        const primaryFile = `${state.id}-${state.name}.png`;
        failures.push(
          ...(await pageViolations(scenarioPage)).map(
            (violation) => `${label}: ${violation}`,
          ),
        );
        await scenarioPage.screenshot({
          path: resolve(directory, primaryFile),
        });
        captured.push(`${viewport.width}/${primaryFile}`);

        if (state.secondary) {
          await state.secondary.prepare(scenarioPage);
          await state.secondary.expect(scenarioPage);
          failures.push(
            ...(await pageViolations(scenarioPage)).map(
              (violation) =>
                `${state.id}b-${state.name}@${viewport.width}: ${violation}`,
            ),
          );
          await scenarioPage.screenshot({
            path: resolve(directory, `${state.secondary.fileName}.png`),
          });
          captured.push(`${viewport.width}/${state.secondary.fileName}.png`);
        }

        if (runtime.unexpectedRequests.length > 0) {
          failures.push(
            `${label}: requêtes non prévues (${runtime.unexpectedRequests.join(", ")})`,
          );
        }
      } catch (error) {
        failures.push(
          `${label}: ${error instanceof Error ? error.message : String(error)}`,
        );
      } finally {
        await scenarioPage.close();
      }
    }
  }

  expect(failures, failures.join("\n")).toEqual([]);
  expect(
    captured,
    `captures manquantes: ${expectedPrimary + expectedSecondary - captured.length}`,
  ).toHaveLength(expectedPrimary + expectedSecondary);
});

/* ------------------------------------------------------------------ */
/* F6-C targeted regressions                                           */
/* ------------------------------------------------------------------ */

test("F6-C — la barre d’actions ne recouvre pas le bloc suivant", async ({
  page,
}) => {
  test.setTimeout(240_000);
  const figureBytes = await readFile(
    new URL("./fixtures/figure-gallery-thumbnail.jpg", import.meta.url),
  );
  await installSyntheticRoutes(page, {}, figureBytes);

  for (const width of [1024, 1280, 1920]) {
    const height = width === 1920 ? 1080 : 900;
    await page.setViewportSize({ width, height });
    await page.goto(`/subjects/${subjectId}/editor`);
    await expect(page.locator(".editorial-workspace")).toBeVisible({
      timeout: 20_000,
    });

    // One-row bar: the section heading exposes Discuter / ···.
    const heading = page.locator(headingSelector);
    const lead = page.locator(leadSelector);
    await heading.click();
    const headingBar = heading.getByRole("toolbar");
    await expect(headingBar).toBeVisible();
    const headingBarBox = await stableBoundingBox(headingBar);
    const leadBox = await stableBoundingBox(lead);
    expect(headingBarBox, `barre absente @${width}`).not.toBeNull();
    expect(leadBox, `chapeau absent @${width}`).not.toBeNull();
    if (!headingBarBox || !leadBox) continue;
    expect(
      headingBarBox.y + headingBarBox.height,
      `la barre recouvre le chapeau @${width}`,
    ).toBeLessThanOrEqual(leadBox.y + 1);

    // A click on the first line of the following one-line paragraph must select
    // that paragraph; Playwright fails if the bar intercepts the pointer.
    await lead.click({ position: { x: 24, y: 12 } });
    await expect(lead).toHaveAttribute("data-selected", "true");
    await expect(heading).toHaveAttribute("data-selected", "false");

    // Wrapped two-row bar: the chart exposes eight actions.
    const chart = page.locator(chartSelector);
    await chart.click();
    const chartBar = chart.getByRole("toolbar");
    await expect(chartBar).toBeVisible();
    const chartBarBox = await stableBoundingBox(chartBar);
    const chartBlockBox = await stableBoundingBox(chart);
    const figureBox = await stableBoundingBox(page.locator(figureASelector));
    expect(chartBarBox, `barre visuelle absente @${width}`).not.toBeNull();
    expect(chartBlockBox).not.toBeNull();
    expect(figureBox).not.toBeNull();
    if (!chartBarBox || !chartBlockBox || !figureBox) continue;
    expect(
      chartBarBox.y + chartBarBox.height,
      `la barre visuelle dépasse son bloc @${width}`,
    ).toBeLessThanOrEqual(chartBlockBox.y + chartBlockBox.height + 1);
    expect(
      chartBarBox.y + chartBarBox.height,
      `la barre visuelle recouvre la figure suivante @${width}`,
    ).toBeLessThanOrEqual(figureBox.y + 1);
    if (width === 1024) {
      const firstButton = await stableBoundingBox(
        chartBar.locator(":scope > .button").first(),
      );
      const lastButton = await stableBoundingBox(
        chartBar.locator(":scope > .button").last(),
      );
      expect(firstButton).not.toBeNull();
      expect(lastButton).not.toBeNull();
      if (firstButton && lastButton) {
        expect(
          lastButton.y,
          "la barre visuelle doit occuper deux lignes à 1024 px",
        ).toBeGreaterThan(firstButton.y + 1);
      }
    }

    // The following figure block stays clickable while the chart is selected.
    const figureA = page.locator(figureASelector);
    await figureA.click({ position: { x: 24, y: 12 } });
    await expect(figureA).toHaveAttribute("data-selected", "true");
    await expect(chart).toHaveAttribute("data-selected", "false");
  }
});

test("F6-C — le focus du mode déplacement reste dans le viewport", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const figureBytes = await readFile(
    new URL("./fixtures/figure-gallery-thumbnail.jpg", import.meta.url),
  );
  await page.setViewportSize({ width: 1024, height: 800 });
  await installSyntheticRoutes(page, {}, figureBytes);
  await page.goto(`/subjects/${subjectId}/editor`);
  await expect(page.locator(".editorial-workspace")).toBeVisible({
    timeout: 20_000,
  });

  // Start from a visual positioned below the initial fold.
  const figure = page.locator(figureBSelector);
  await figure.scrollIntoViewIfNeeded();
  await expect(figure).toBeInViewport();
  const scrollY = Number(await page.evaluate("window.scrollY"));
  expect(
    scrollY,
    "le visuel de départ doit être sous le pli initial",
  ).toBeGreaterThan(0);

  await figure.click();
  const moveButton = figure.getByRole("button", {
    name: "Déplacer",
    exact: true,
  });
  await expect(moveButton).toBeVisible();
  await moveButton.click();

  const banner = page.getByRole("region", {
    name: "Déplacement du visuel",
  });
  await expect(banner).toBeFocused();
  await expect(banner).toBeInViewport({ ratio: 0.5 });

  const topbarBox = await page.locator(".editorial-topbar").boundingBox();
  const bannerBox = await banner.boundingBox();
  expect(topbarBox).not.toBeNull();
  expect(bannerBox).not.toBeNull();
  if (topbarBox && bannerBox) {
    expect(
      bannerBox.y,
      "le bandeau ne doit pas être masqué par la barre supérieure",
    ).toBeGreaterThanOrEqual(topbarBox.y + topbarBox.height - 1);
  }

  const outlineJson = await page.evaluate(
    `(() => {
       const element = document.querySelector('.visual-move-banner');
       if (!element) return null;
       const style = window.getComputedStyle(element);
       return JSON.stringify({
         width: style.outlineWidth,
         style: style.outlineStyle,
       });
     })()`,
  );
  const outline =
    typeof outlineJson === "string"
      ? (JSON.parse(outlineJson) as { width: string; style: string })
      : null;
  expect(
    outline,
    "bandeau introuvable pour le contrôle de focus",
  ).not.toBeNull();
  if (outline) {
    expect(
      Number.parseFloat(outline.width),
      "anneau de focus visible sur le bandeau",
    ).toBeGreaterThanOrEqual(3);
    expect(outline.style).not.toBe("none");
  }
});
