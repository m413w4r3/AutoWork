import { constants } from "node:fs";
import { access, mkdir, readFile, rm, stat } from "node:fs/promises";
import { resolve } from "node:path";

import {
  expect,
  test,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";

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

const paragraphId = "22222222-2222-4222-8222-222222222222";
const sectionId = "11111111-1111-4111-8111-111111111111";
const titleId = "00000000-0000-4000-8000-000000000000";
const artifactId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const threadId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const proposalId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const historicalArtifactId = "99999999-9999-4999-8999-999999999999";
const publicationId = "abababab-abab-4bab-8bab-abababababab";
const catalogFigureId = "88888888-8888-4888-8888-888888888888";
const canonicalSha256 = "a".repeat(64);
const conflictMessage =
  "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition.";
const screenshotDirectory = resolve(
  process.cwd(),
  "test-results/editorial-workspace",
);

type VisualState =
  | "document-ready-selected"
  | "inline-edit"
  | "bridge-generation"
  | "proposal-diff"
  | "table-inserted"
  | "chart-inserted"
  | "figure-gallery-selected"
  | "figures-inserted"
  | "version-conflict-409"
  | "pdf-stale"
  | "proposal-history"
  | "historical-read-only"
  | "keyboard-focus-visible"
  | "bridge-session-lost";

interface ViewportSpec {
  label: "1440x1000" | "1180x900";
  width: number;
  height: number;
}

interface Scenario {
  state: VisualState;
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  thread: EditorialThreadRead;
  proposal: EditorialProposalRead;
  history: EditorialHistoryItem[];
}

interface ScrollBounds {
  getBoundingClientRect(): { top: number };
}

interface ScrollableBounds extends ScrollBounds {
  scrollTop: number;
}

const syntheticEvidence: EditorialEvidenceReference = {
  source_document_id: "12121212-1212-4121-8121-121212121212",
  kind: "fact",
  evidence_key: "a".repeat(64),
};

const viewports: ViewportSpec[] = [
  { label: "1440x1000", width: 1440, height: 1000 },
  { label: "1180x900", width: 1180, height: 900 },
];

const visualStates: VisualState[] = [
  "document-ready-selected",
  "inline-edit",
  "bridge-generation",
  "proposal-diff",
  "table-inserted",
  "chart-inserted",
  "figure-gallery-selected",
  "figures-inserted",
  "version-conflict-409",
  "pdf-stale",
  "proposal-history",
  "historical-read-only",
  "keyboard-focus-visible",
  "bridge-session-lost",
];

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
    parent_block_id:
      kind === "title" || kind === "section_heading" ? null : sectionId,
    position,
    text,
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: kind === "paragraph" ? canonicalSha256 : null,
    allowed_operations: [],
    revision_status: "ready",
    ...overrides,
  };
}

function insertedTable(anchorBlockId: string): EditorialWorkspaceBlock {
  const evidence = {
    ...syntheticEvidence,
    source_title: "Source synthétique",
    passage: "Valeur synthétique rapportée pour cette revue visuelle.",
  };
  return block(
    "55555555-5555-4555-8555-555555555555",
    "table",
    3,
    "Résumé synthétique",
    {
      evidence_refs: [evidence],
      table: {
        key: "visual-review-table",
        kind: "custom",
        title: "Résumé synthétique",
        caption: "Tableau de démonstration synthétique",
        columns: [
          { key: "element", label: "Élément" },
          { key: "value", label: "Valeur rapportée" },
        ],
        rows: [
          {
            cells: ["Premier élément", "Valeur 4"],
            evidence_refs: [syntheticEvidence],
          },
          {
            cells: ["Second élément", "Valeur 8"],
            evidence_refs: [syntheticEvidence],
          },
        ],
        evidence_refs: [syntheticEvidence],
        anchor_block_id: anchorBlockId,
        placement: "after_paragraph",
      },
    },
  );
}

function insertedChart(anchorBlockId: string): EditorialWorkspaceBlock {
  const evidence = {
    ...syntheticEvidence,
    source_title: "Source synthétique",
    passage: "Valeurs de démonstration synthétiques.",
  };
  return block(
    "66666666-6666-4666-8666-666666666666",
    "chart",
    3,
    "Valeurs documentées",
    {
      evidence_refs: [evidence],
      chart: {
        key: "visual-review-chart",
        kind: "bar",
        title: "Valeurs documentées",
        caption: "Graphique de démonstration synthétique",
        legend: ["Mesures synthétiques"],
        category_axis_label: "Élément",
        value_axis_label: "Valeur rapportée",
        unit: "unités",
        points: [
          { label: "Premier", value: "4", evidence_refs: [syntheticEvidence] },
          { label: "Second", value: "8", evidence_refs: [syntheticEvidence] },
        ],
        anchor_block_id: anchorBlockId,
      },
    },
  );
}

function insertedFigure(anchorBlockId: string): EditorialWorkspaceBlock {
  return block(
    "77777777-7777-4777-8777-777777777777",
    "figure",
    3,
    "Illustration synthétique des échanges",
    {
      figure: {
        key: "adopted_source_figure_visualreview",
        catalog_figure_id: catalogFigureId,
        caption: "Illustration synthétique des échanges",
        source_url: "https://publisher.example/synthetic-source",
        source: "Rapport public synthétique",
        provenance: "Figure archivée et vérifiée dans une source synthétique.",
        sha256: "b".repeat(64),
        mime_type: "image/jpeg",
        byte_size: 4096,
        width: 1280,
        height: 720,
        status: "included",
        order: 1,
        anchor_block_id: anchorBlockId,
        placement: "after_paragraph",
        page: 2,
        section: "Contexte",
      },
    },
  );
}

function makeSnapshot(
  subjectId: string,
  state: VisualState,
): EditorialWorkspaceSnapshot {
  const paragraph = block(
    paragraphId,
    "paragraph",
    2,
    "Ce paragraphe synthétique décrit un exemple de mise en page éditoriale. Il permet de vérifier la lecture, les actions et les sources associées.",
    {
      evidence_refs: [
        {
          source_title: "Source synthétique",
          passage: "Passage de démonstration sans donnée réelle.",
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
    },
  );
  const contentBlocks = [
    block(titleId, "title", 0, "Article synthétique"),
    block(sectionId, "section_heading", 1, "Contexte"),
    paragraph,
    block(
      "22222222-2222-4222-8222-222222222223",
      "paragraph",
      4,
      "Un second paragraphe synthétique apporte un peu de longueur au document et aide à juger la place laissée au contenu éditorial.",
    ),
    block(
      "22222222-2222-4222-8222-222222222224",
      "paragraph",
      5,
      "La dernière phrase reste fictive et sert uniquement à observer la hiérarchie visuelle de l’atelier.",
    ),
  ];

  if (state === "table-inserted") {
    contentBlocks.splice(3, 0, insertedTable(paragraphId));
  }
  if (state === "chart-inserted") {
    contentBlocks.splice(3, 0, insertedChart(paragraphId));
  }
  if (state === "figures-inserted") {
    contentBlocks.splice(3, 0, insertedFigure(paragraphId));
  }

  const isStalePdf = state === "pdf-stale";
  const addedBlocks = contentBlocks.filter((item) =>
    ["table", "chart", "figure"].includes(item.kind),
  );
  return {
    subject_id: subjectId,
    production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    publication_kind: "article",
    synthesis_artifact_id: artifactId,
    synthesis_version: 4,
    synthesis_canonical_sha256: canonicalSha256,
    enrichment_artifact_id:
      addedBlocks.length > 0 ? "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" : null,
    enrichment_version: addedBlocks.length > 0 ? 2 : null,
    publication_artifact_id: isStalePdf ? null : publicationId,
    publication_version: isStalePdf ? null : 4,
    pipeline_generation: 1,
    blocks: contentBlocks,
    enrichments: addedBlocks,
    document_outline: [
      {
        block_id: sectionId,
        label: "Contexte",
        children: contentBlocks
          .filter((item) => item.parent_block_id === sectionId)
          .map((item) => item.block_id),
      },
    ],
    capabilities: {
      can_discuss: true,
      can_preview_revision: false,
      manual_edit: true,
      can_add_enrichment: true,
      can_preview_pdf: true,
      can_regenerate_pdf: true,
    },
    preview_status: isStalePdf ? "STALE" : "READY",
    impact_status: "none",
    pdf_status: isStalePdf ? "STALE" : "CURRENT",
    pdf_artifact_id: isStalePdf ? null : publicationId,
    pdf_artifact_version: isStalePdf ? null : 4,
    pdf_artifact_input_hash: isStalePdf ? null : "c".repeat(64),
    pdf_artifact_canonical_sha256: isStalePdf ? null : "d".repeat(64),
    pdf_render_id: isStalePdf ? null : "acacacac-acac-4cac-8cac-acacacacacac",
    pdf_render_identity: isStalePdf ? null : "synthetic-render-v4",
    pdf_error_code: null,
    pdf_error_message: null,
    pdf_url: isStalePdf
      ? null
      : `/api/subjects/${subjectId}/editor/pdf/download?version=4`,
    snapshot_id: "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd",
  };
}

function figureCatalogItem(): EditorialFigureCatalogItem {
  return {
    figure_id: catalogFigureId,
    original_name: "Carte synthétique des échanges",
    original_caption: "Carte de démonstration issue d’une source synthétique.",
    alt_text: "Carte synthétique des échanges",
    source: {
      source_id: "12121212-1212-4121-8121-121212121212",
      title: "Rapport public synthétique",
      url: "https://publisher.example/synthetic-source",
    },
    page: 2,
    section: "Contexte",
    context_before: "Contexte synthétique précédent.",
    context_after: "Contexte synthétique suivant.",
    width: 1280,
    height: 720,
    mime_type: "image/jpeg",
    status: "available",
    selectable: true,
    exclusion_reason: null,
    already_used: false,
  };
}

function makeProposal(
  subjectId: string,
  snapshot: EditorialWorkspaceSnapshot,
): EditorialProposalRead {
  const baseText = snapshot.blocks.find(
    (item) => item.block_id === paragraphId,
  )?.text;
  if (!baseText) throw new Error("The paragraph fixture is missing");
  return {
    id: proposalId,
    subject_id: subjectId,
    production_run_id: snapshot.production_run_id,
    occurrence_id: paragraphId,
    thread_id: threadId,
    origin: "bridge_draft",
    state: "proposed",
    state_version: 1,
    operation: "rewrite_paragraph",
    base_artifact_id: artifactId,
    base_artifact_version: 4,
    base_canonical_sha256: canonicalSha256,
    base_text: baseText,
    patch_sha256: "e".repeat(64),
    patch: {
      schema_version: "editorial-patch-v1",
      operation: "rewrite_paragraph",
      target: { block_id: paragraphId },
      target_id: paragraphId,
      occurrence_id: paragraphId,
      before_hash: canonicalSha256,
      rationale: "Proposition synthétique de démonstration.",
      after:
        "Cette version synthétique montre le paragraphe proposé dans une comparaison éditoriale claire.",
    },
    diagnostics: {},
    created_at: "2026-11-20T10:00:00Z",
  };
}

function makeHistoryItem(subjectId: string): EditorialHistoryItem {
  return {
    proposal_id: proposalId,
    subject_id: subjectId,
    occurrence_id: paragraphId,
    state: "accepted",
    origin: "manual_patch",
    operation: "rewrite_paragraph",
    operation_label: "Réécriture",
    actor_id: "operator-synthetic",
    actor_display: "Opérateur synthétique",
    created_at: "2026-11-20T09:30:00Z",
    updated_at: "2026-11-20T09:30:01Z",
    canonical_result_artifact_id: artifactId,
    canonical_result_version: 5,
    canonical_result_stage: "synthesis",
    thread_id: threadId,
    thread_url: `/api/subjects/${subjectId}/editor/threads/${paragraphId}`,
  };
}

function timelineEvent(
  id: string,
  sequence: number,
  kind: EditorialThreadTimelineEvent["kind"],
  body: string | null,
  proposal: string | null = null,
  reconciliationOutcome?: EditorialThreadTimelineEvent["reconciliation_outcome"],
): EditorialThreadTimelineEvent {
  return {
    id,
    sequence,
    kind,
    actor_id: null,
    proposal_id: proposal,
    created_at: "2026-11-20T10:00:00Z",
    body,
    ...(reconciliationOutcome
      ? { reconciliation_outcome: reconciliationOutcome }
      : {}),
  };
}

function makeScenario(state: VisualState, sequence: number): Scenario {
  const subjectId = `80000000-0000-4000-8000-${String(sequence).padStart(12, "0")}`;
  const snapshot = makeSnapshot(subjectId, state);
  const binding: EditorialThreadBinding = {
    subject_id: subjectId,
    production_run_id: snapshot.production_run_id,
    occurrence_id: paragraphId,
    artifact_id: artifactId,
    artifact_version: 4,
    canonical_sha256: canonicalSha256,
  };
  const openThread = (
    overrides: Partial<EditorialThreadOpenRead>,
  ): EditorialThreadOpenRead => ({
    kind: "open",
    thread_id: threadId,
    origin: binding,
    current: binding,
    binding_state: "valid",
    previous_sessions: [],
    bridge_status: "ready",
    event_sequence: 0,
    timeline: [],
    etag: `"visual-${state}"`,
    ...overrides,
  });

  let thread: EditorialThreadRead = {
    kind: "unopened",
    binding,
    current: binding,
    bridge_status: "ready",
    event_sequence: 0,
    timeline: [],
    etag: `"visual-${state}"`,
  };
  if (state === "bridge-generation") {
    thread = openThread({
      bridge_status: "generating",
      event_sequence: 1,
      timeline: [
        timelineEvent(
          "event-synthetic-user",
          1,
          "user_message",
          "Clarifier ce paragraphe synthétique.",
        ),
      ],
    });
  } else if (state === "proposal-diff" || state === "version-conflict-409") {
    thread = openThread({
      bridge_status: "reply_available",
      event_sequence: 1,
      timeline: [
        timelineEvent(
          "event-synthetic-proposal",
          1,
          "proposal",
          null,
          proposalId,
        ),
      ],
    });
  } else if (state === "bridge-session-lost") {
    thread = openThread({
      bridge_status: "session_lost",
      event_sequence: 1,
      timeline: [
        timelineEvent(
          "event-synthetic-session-lost",
          1,
          "reconciliation",
          "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre",
          null,
          "session_lost",
        ),
      ],
    });
  }

  return {
    state,
    subjectId,
    snapshot,
    thread,
    proposal: makeProposal(subjectId, snapshot),
    history: [makeHistoryItem(subjectId)],
  };
}

async function installSyntheticRoutes(
  page: Page,
  scenario: Scenario,
): Promise<string[]> {
  const unexpectedRequests: string[] = [];
  const subject = {
    ...EDITORIAL_FIXTURES.subject,
    id: scenario.subjectId,
  };
  const subjectPath = `/api/subjects/${scenario.subjectId}`;
  const threadPath = `${subjectPath}/editor/threads/${paragraphId}`;
  const proposalPath = `${threadPath}/proposals/${proposalId}`;
  const catalogItem = figureCatalogItem();
  const figureBytes = await readFile(
    new URL("./fixtures/figure-gallery-thumbnail.jpg", import.meta.url),
  );

  await page.route("**/*", async (route: Route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    if (url.origin !== EDITORIAL_APP_ORIGIN) {
      unexpectedRequests.push(`${method} ${url.origin}${url.pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    if (
      await fulfillEditorialReadStatuses(
        route,
        method,
        url.pathname,
        scenario.snapshot,
      )
    ) {
      return;
    }
    if (method === "GET" && url.pathname === subjectPath) {
      await route.fulfill({ json: subject });
      return;
    }
    if (method === "GET" && url.pathname === `${subjectPath}/content`) {
      await route.fulfill({ status: 404, body: "" });
      return;
    }
    if (method === "GET" && url.pathname === `${subjectPath}/editor/snapshot`) {
      await route.fulfill({ json: scenario.snapshot });
      return;
    }
    if (
      method === "GET" &&
      new RegExp(`^${subjectPath}/editor/blocks/[^/]+/draft$`).test(
        url.pathname,
      )
    ) {
      await route.fulfill({
        status: 404,
        json: { detail: { code: "draft_not_found" } },
      });
      return;
    }
    if (method === "GET" && url.pathname === threadPath) {
      const etag = scenario.thread.etag;
      if (route.request().headers()["if-none-match"] === etag) {
        await route.fulfill({ status: 304, headers: { ETag: etag } });
        return;
      }
      const afterSequence = Number(
        url.searchParams.get("after_sequence") ?? "0",
      );
      const timeline = scenario.thread.timeline.filter(
        (item) => item.sequence > afterSequence,
      );
      await route.fulfill({
        status: 200,
        headers: { ETag: etag },
        json: { ...scenario.thread, timeline },
      });
      return;
    }
    if (method === "GET" && url.pathname === proposalPath) {
      await route.fulfill({ json: scenario.proposal });
      return;
    }
    if (method === "POST" && url.pathname === `${proposalPath}/accept`) {
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
    if (method === "GET" && url.pathname === `${subjectPath}/editor/history`) {
      await route.fulfill({
        json: { items: scenario.history, next_cursor: null, limit: 20 },
      });
      return;
    }
    if (method === "GET" && url.pathname === `${subjectPath}/editor/figures`) {
      await route.fulfill({
        json: {
          items: [catalogItem],
          sources: [catalogItem.source],
          total: 1,
          available_count: 1,
          offset: Number(url.searchParams.get("offset") ?? 0),
          limit: Number(url.searchParams.get("limit") ?? 30),
        },
      });
      return;
    }
    if (
      method === "GET" &&
      url.pathname ===
        `${subjectPath}/editor/figures/${catalogFigureId}/thumbnail`
    ) {
      await route.fulfill({
        status: 200,
        contentType: "image/jpeg",
        body: figureBytes,
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
          production_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          subject_id: scenario.subjectId,
          stage: "publication",
          version: 3,
          input_hash: "f".repeat(64),
          canonical_sha256: "a".repeat(64),
          status: "verified",
          metadata: {},
          publication_kind: "article",
          read_only_reason: "historical_version",
          canonical_content: {
            schema_version: "4",
            subject_id: scenario.subjectId,
            publication_language: "fr",
            title: "Publication historique synthétique",
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
        },
      });
      return;
    }

    unexpectedRequests.push(`${method} ${url.pathname}${url.search}`);
    await route.abort("blockedbyclient");
  });

  return unexpectedRequests;
}

function mainAction(page: Page, state: VisualState): Locator {
  if (
    state === "document-ready-selected" ||
    state === "keyboard-focus-visible"
  ) {
    return page
      .locator(".editorial-block-actions")
      .getByRole("button", { name: "Modifier", exact: true });
  }
  if (state === "inline-edit") {
    return page.getByRole("button", { name: "Enregistrer", exact: true });
  }
  if (state === "bridge-generation") {
    return page
      .getByRole("status")
      .filter({ hasText: "Bridge traite votre demande." });
  }
  if (state === "proposal-diff") {
    return page.getByRole("button", { name: "Accepter", exact: true });
  }
  if (state === "version-conflict-409") {
    return page.getByRole("button", {
      name: "Comparer à nouveau",
      exact: true,
    });
  }
  if (state === "figure-gallery-selected") {
    return page.getByRole("checkbox", {
      name: "Sélectionner Carte synthétique des échanges",
    });
  }
  if (state === "pdf-stale") {
    return page.getByRole("button", { name: "Régénérer le PDF", exact: true });
  }
  if (state === "proposal-history") {
    return page.getByRole("button", { name: "Fermer l’historique" });
  }
  if (state === "historical-read-only") {
    return page.getByRole("link", {
      name: "Revenir à la version courante",
    });
  }
  if (state === "bridge-session-lost") {
    return page.getByRole("button", {
      name: "Démarrer une nouvelle discussion",
    });
  }
  return page.getByRole("link", { name: "Voir le PDF", exact: true });
}

async function tabTo(page: Page, target: Locator) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await page.keyboard.press("Tab");
    try {
      await expect(target).toBeFocused({ timeout: 100 });
      return;
    } catch {
      // Continue through the document's native tab order.
    }
  }
  throw new Error("Keyboard tab order did not reach the requested control");
}

async function expectProposalCard(page: Page) {
  const card = page
    .getByRole("article", { name: "Proposition éditoriale" })
    .first();
  await expect(card).toBeVisible();
  await expect(
    card.getByRole("region", { name: "Comparaison éditoriale" }),
  ).toBeVisible();
  await expect(
    card.getByRole("region", { name: "Version actuelle" }),
  ).toBeVisible();
  await expect(card.getByRole("region", { name: "Proposition" })).toBeVisible();
  return card;
}

async function positionProposalCard(page: Page) {
  const card = page
    .getByRole("article", { name: "Proposition éditoriale" })
    .first();
  const cardHandle = await card.elementHandle();
  if (!cardHandle) throw new Error("The proposal card is not rendered");
  const inspector = page.locator(".editorial-workspace__inspector-column");
  await inspector.evaluate((container, target) => {
    const scrollable = container as unknown as ScrollableBounds;
    const element = target as unknown as ScrollBounds;
    scrollable.scrollTop +=
      element.getBoundingClientRect().top -
      scrollable.getBoundingClientRect().top;
  }, cardHandle);
}

async function prepareState(page: Page, state: VisualState) {
  if (state === "historical-read-only") return;

  const selectedBlock = page.locator(
    `.editorial-document__block[data-block-id="${paragraphId}"]`,
  );
  await expect(selectedBlock).toHaveAttribute("data-selected", "true");

  if (state === "inline-edit") {
    await page
      .locator(".editorial-block-actions")
      .getByRole("button", { name: "Modifier", exact: true })
      .click();
    await expect(
      page.getByRole("textbox", { name: "Texte du bloc" }),
    ).toBeVisible();
    return;
  }
  if (state === "figure-gallery-selected") {
    await page.getByRole("button", { name: "Images sources" }).click();
    const gallery = page.getByRole("dialog", {
      name: "Galerie d’images sources",
    });
    await expect(gallery).toBeVisible();
    const checkbox = gallery.getByRole("checkbox", {
      name: "Sélectionner Carte synthétique des échanges",
    });
    await expect(checkbox).toBeVisible();
    await checkbox.check();
    await expect(checkbox).toBeChecked();
    await expect(gallery.getByText("1 image sélectionnée")).toBeVisible();
    return;
  }
  if (state === "proposal-diff") {
    await expectProposalCard(page);
    await positionProposalCard(page);
    return;
  }
  if (state === "version-conflict-409") {
    await expectProposalCard(page);
    await page.getByRole("button", { name: "Accepter", exact: true }).click();
    await expect(
      page.getByText(conflictMessage, { exact: true }),
    ).toBeVisible();
    await positionProposalCard(page);
    return;
  }
  if (state === "proposal-history") {
    await page.getByRole("button", { name: "Historique et détails" }).click();
    await page.getByRole("button", { name: "Historique", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Historique des propositions" }),
    ).toBeVisible();
    return;
  }
  if (state === "pdf-stale") {
    await expect(page.locator(".editorial-status")).toHaveText("PDF obsolète");
    return;
  }
  if (state === "bridge-generation") {
    await expect(
      page.getByText(
        "Bridge traite votre demande. L’état sera mis à jour dans ce fil.",
      ),
    ).toBeVisible();
    return;
  }
  if (state === "bridge-session-lost") {
    await expect(
      page
        .getByRole("status")
        .filter({ hasText: "Cette session Bridge est perdue." }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Démarrer une nouvelle discussion" }),
    ).toBeVisible();
    return;
  }
  if (state === "keyboard-focus-visible") {
    const modify = page
      .locator(".editorial-block-actions")
      .getByRole("button", { name: "Modifier", exact: true });
    await selectedBlock.focus();
    await tabTo(page, modify);
    await expect(modify).toBeFocused();
    const showsKeyboardFocus = await modify.evaluate((element) =>
      (element as unknown as { matches(selector: string): boolean }).matches(
        ":focus-visible",
      ),
    );
    expect(showsKeyboardFocus).toBe(true);
  }
}

test("test_visual_review_captures_desktop_and_states", async ({ page }) => {
  test.setTimeout(240_000);
  await rm(screenshotDirectory, { recursive: true, force: true });
  await mkdir(screenshotDirectory, { recursive: true });
  const context = page.context();
  const failures: string[] = [];
  const capturedFiles: string[] = [];
  let sequence = 1;

  for (const viewport of viewports) {
    for (const state of visualStates) {
      const scenario = makeScenario(state, sequence);
      sequence += 1;
      const scenarioPage = await context.newPage();
      try {
        scenarioPage.setDefaultTimeout(7_000);
        const unexpectedRequests = await installSyntheticRoutes(
          scenarioPage,
          scenario,
        );
        await scenarioPage.setViewportSize({
          width: viewport.width,
          height: viewport.height,
        });
        const isHistorical = state === "historical-read-only";
        const query = isHistorical
          ? `?publication_artifact_id=${historicalArtifactId}`
          : `?thread=${paragraphId}`;
        await scenarioPage.goto(
          `/subjects/${scenario.subjectId}/editor${query}`,
        );

        if (isHistorical) {
          await expect(
            scenarioPage.getByRole("heading", {
              name: "Atelier éditorial — version 3",
            }),
          ).toBeVisible();
          await expect(
            scenarioPage.getByText("Version historique — lecture seule"),
          ).toBeVisible();
        } else {
          await expect(
            scenarioPage.locator(".editorial-workspace__grid"),
          ).toBeVisible();
        }

        await prepareState(scenarioPage, state);
        const action = mainAction(scenarioPage, state);
        await expect(
          action,
          `${state}/${viewport.label}: action principale absente`,
        ).toBeVisible();
        await action.scrollIntoViewIfNeeded();
        await expect(
          action,
          `${state}/${viewport.label}: action hors viewport`,
        ).toBeInViewport();

        const noHorizontalOverflow = Boolean(
          await scenarioPage.evaluate(
            "document.scrollingElement && document.scrollingElement.scrollWidth <= document.scrollingElement.clientWidth",
          ),
        );
        if (!noHorizontalOverflow) {
          failures.push(
            `${state}/${viewport.label}: débordement horizontal de page`,
          );
        }

        const visibleMainText = await scenarioPage
          .getByRole("main")
          .innerText();
        if (/\bundefined\b|\[object/i.test(visibleMainText)) {
          failures.push(`${state}/${viewport.label}: chaîne technique visible`);
        }
        if (
          /\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b/i.test(
            visibleMainText,
          )
        ) {
          failures.push(`${state}/${viewport.label}: UUID brut visible`);
        }
        if (unexpectedRequests.length > 0) {
          failures.push(
            `${state}/${viewport.label}: requêtes non prévues (${unexpectedRequests.join(", ")})`,
          );
        }

        const fileName = `${state}-${viewport.label}.png`;
        const screenshotPath = resolve(screenshotDirectory, fileName);
        await scenarioPage.screenshot({ path: screenshotPath });
        await access(screenshotPath, constants.F_OK);
        const screenshotStat = await stat(screenshotPath);
        if (screenshotStat.size === 0) {
          failures.push(`${state}/${viewport.label}: capture vide`);
        }
        capturedFiles.push(fileName);
      } finally {
        await scenarioPage.close();
      }
    }
  }

  expect(
    capturedFiles,
    `captures manquantes: ${visualStates.length * viewports.length - capturedFiles.length}`,
  ).toHaveLength(visualStates.length * viewports.length);
  expect(failures, failures.join("\n")).toEqual([]);
});
