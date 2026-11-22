import type { Page, Route } from "@playwright/test";

import type {
  EditorialEnrichmentElementKind,
  EditorialEnrichmentRevisionInput,
  EditorialProposalAcceptResult,
  EditorialProposalOperation,
  EditorialProposalRead,
  EditorialProposalState,
  EditorialThreadRead,
  EditorialVisualRevisionPatch,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../../src/api/editorialWorkspace";
import {
  EDITORIAL_APP_ORIGIN,
  EDITORIAL_FIXTURES,
  fulfillEditorialReadStatuses,
} from "./editorialApi";

export const VISUAL_SUBJECT_ID = EDITORIAL_FIXTURES.subject.id;
export const VISUAL_RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
export const VISUAL_ARTIFACT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
export const VISUAL_CANONICAL_SHA = "a".repeat(64);
export const VISUAL_SNAPSHOT_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
export const VISUAL_THREAD_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
export const CHART_KEY = "chart-a";
export const FIGURE_A_KEY = "figure-a";
export const FIGURE_B_KEY = "figure-b";
export const DIAGRAM_KEY = "diagram-a";
export const PARAGRAPH_COUNT = 6;

type ElementKey =
  | typeof CHART_KEY
  | typeof FIGURE_A_KEY
  | typeof FIGURE_B_KEY
  | typeof DIAGRAM_KEY;

const ELEMENT_KIND: Record<ElementKey, EditorialEnrichmentElementKind> = {
  [CHART_KEY]: "chart",
  [FIGURE_A_KEY]: "figure",
  [FIGURE_B_KEY]: "figure",
  [DIAGRAM_KEY]: "diagram",
};

const ELEMENT_TITLE: Record<ElementKey, string> = {
  [CHART_KEY]: "Graphique de synthèse",
  [FIGURE_A_KEY]: "Figure A",
  [FIGURE_B_KEY]: "Figure B",
  [DIAGRAM_KEY]: "Diagramme de synthèse",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

function paragraphNumber(anchorBlockId: string): number | null {
  const match = /^paragraph-(\d+)-v\d+$/.exec(anchorBlockId);
  return match ? Number(match[1]) : null;
}

export class VisualEditorialFixture {
  version = 0;
  placements: Record<ElementKey, string> = {
    [CHART_KEY]: "p2",
    [FIGURE_A_KEY]: "p2",
    [FIGURE_B_KEY]: "p2",
    [DIAGRAM_KEY]: "p5",
  };
  orders: Record<ElementKey, number> = {
    [CHART_KEY]: 0,
    [FIGURE_A_KEY]: 1,
    [FIGURE_B_KEY]: 2,
    [DIAGRAM_KEY]: 0,
  };
  captions: Record<ElementKey, string> = {
    [CHART_KEY]: "Légende initiale du graphique",
    [FIGURE_A_KEY]: "Figure A — légende éditoriale",
    [FIGURE_B_KEY]: "Figure B — légende éditoriale",
    [DIAGRAM_KEY]: "Légende initiale du diagramme",
  };
  originalCaptions: Partial<Record<ElementKey, string>> = {
    [FIGURE_A_KEY]: "Légende archivée de la figure A",
    [FIGURE_B_KEY]: "Légende archivée de la figure B",
  };
  chartTitle = "Titre initial du graphique";
  chartPointOne = "3";
  diagramNodeLabel = "Nœud initial";
  removed = new Set<ElementKey>();
  readOnly = false;
  canDiscuss = true;

  paragraphBlockId(index: number): string {
    return `paragraph-${index}-v${this.version}`;
  }

  elementBlockId(key: ElementKey): string {
    return `${ELEMENT_KIND[key]}-${key}-v${this.version}`;
  }

  anchorBlockIdFor(key: ElementKey): string {
    const placement = this.placements[key];
    const number = Number(placement.slice(1));
    return this.paragraphBlockId(number);
  }

  activeKeys(): ElementKey[] {
    return (Object.keys(ELEMENT_KIND) as ElementKey[]).filter(
      (key) => !this.removed.has(key),
    );
  }

  private operationsFor(key: ElementKey): string[] {
    if (this.readOnly) return [];
    const kind = ELEMENT_KIND[key];
    const operations = ["update_visual_caption", "move_enrichment_element"];
    if (kind === "chart") operations.push("rewrite_chart", "delete_chart");
    if (kind === "diagram")
      operations.push("rewrite_diagram", "delete_diagram");
    if (kind === "figure") {
      operations.push("replace_source_figure", "delete_source_figure");
    }
    if (this.groupSize(this.placements[key]) >= 2) {
      operations.push("reorder_enrichment_elements");
    }
    return operations;
  }

  private groupSize(placement: string): number {
    return this.activeKeys().filter((key) => this.placements[key] === placement)
      .length;
  }

  private elementBlock(key: ElementKey): EditorialWorkspaceBlock {
    const kind = ELEMENT_KIND[key];
    const anchorBlockId = this.anchorBlockIdFor(key);
    const base = {
      block_id: this.elementBlockId(key),
      kind,
      parent_block_id: null,
      position: 0,
      text: ELEMENT_TITLE[key],
      evidence_refs: [{ label: `Source de ${ELEMENT_TITLE[key]}` }],
      chart: null,
      revision_target: {
        block_id: this.elementBlockId(key),
        legacy_target_id: null,
      },
      before_hash: "b".repeat(64),
      allowed_operations: this.operationsFor(key),
      revision_status: "ready" as const,
      element_kind: kind,
      element_key: key,
      order: this.orders[key],
      caption: this.captions[key],
    };
    if (kind === "chart") {
      return {
        ...base,
        chart: {
          key,
          kind: "bar",
          title: this.chartTitle,
          caption: this.captions[key],
          legend: ["Série A"],
          category_axis_label: "Catégories",
          value_axis_label: "Valeurs",
          unit: "événements",
          points: [
            {
              label: "Catégorie 1",
              value: this.chartPointOne,
              evidence_refs: [
                {
                  source_document_id: "source-1",
                  kind: "fact",
                  evidence_key: "fact-1",
                },
              ],
            },
            {
              label: "Catégorie 2",
              value: "1",
              evidence_refs: [],
            },
          ],
          anchor_block_id: anchorBlockId,
        },
      };
    }
    if (kind === "figure") {
      return {
        ...base,
        figure: {
          key,
          catalog_figure_id: null,
          original_caption: this.originalCaptions[key] ?? null,
          caption: this.captions[key],
          source_url: "https://example.invalid/source",
          source: "Source synthétique",
          provenance: "Provenance synthétique",
          sha256: "c".repeat(64),
          mime_type: "image/png",
          byte_size: 2048,
          width: 320,
          height: 180,
          status: "included",
          order: this.orders[key],
          anchor_block_id: anchorBlockId,
          placement: "after_paragraph",
          page: 1,
          section: "Section synthétique",
        },
      };
    }
    return {
      ...base,
      diagram: {
        key,
        kind: "network_flow",
        title: ELEMENT_TITLE[key],
        caption: this.captions[key],
        direction: "left_to_right",
        profile: null,
        nodes: [
          { node_id: "n1", label: this.diagramNodeLabel, role: null },
          { node_id: "n2", label: "Nœud final", role: null },
        ],
        edges: [{ source_node_id: "n1", target_node_id: "n2", label: null }],
        groups: [],
        anchor_block_id: anchorBlockId,
        placement: "after_paragraph",
      },
    };
  }

  snapshot(): EditorialWorkspaceSnapshot {
    const blocks: EditorialWorkspaceBlock[] = [
      {
        block_id: `title-v${this.version}`,
        kind: "title",
        parent_block_id: null,
        position: 0,
        text: "Article synthétique F3-A",
        evidence_refs: [],
        chart: null,
        revision_target: {
          block_id: `title-v${this.version}`,
          legacy_target_id: null,
        },
        before_hash: "d".repeat(64),
        allowed_operations: this.readOnly ? [] : ["rewrite_title"],
        revision_status: "ready",
      },
    ];
    for (let index = 1; index <= PARAGRAPH_COUNT; index += 1) {
      const anchor = `p${index}`;
      blocks.push({
        block_id: this.paragraphBlockId(index),
        kind: "paragraph",
        parent_block_id: null,
        position: index,
        text: `Paragraphe ${index} du document synthétique.`,
        evidence_refs: [{ label: `Source du paragraphe ${index}` }],
        chart: null,
        revision_target: {
          block_id: this.paragraphBlockId(index),
          legacy_target_id: null,
        },
        before_hash: "e".repeat(64),
        allowed_operations: this.readOnly ? [] : ["rewrite_paragraph"],
        revision_status: "ready",
      });
      const attached = this.activeKeys()
        .filter((key) => this.placements[key] === anchor)
        .sort(
          (left, right) =>
            this.orders[left] - this.orders[right] || left.localeCompare(right),
        );
      for (const key of attached) {
        blocks.push(this.elementBlock(key));
      }
    }
    return {
      subject_id: VISUAL_SUBJECT_ID,
      production_run_id: VISUAL_RUN_ID,
      publication_kind: "article",
      synthesis_artifact_id: VISUAL_ARTIFACT_ID,
      synthesis_version: this.version + 1,
      synthesis_canonical_sha256: VISUAL_CANONICAL_SHA,
      enrichment_artifact_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
      enrichment_version: this.version + 1,
      publication_artifact_id: null,
      publication_version: null,
      pipeline_generation: 1,
      blocks,
      enrichments: blocks.filter((block) => block.element_kind !== null),
      document_outline: [],
      capabilities: {
        can_discuss: this.canDiscuss,
        can_preview_revision: false,
        manual_edit: !this.readOnly,
        can_add_enrichment: false,
        can_preview_pdf: false,
        can_regenerate_pdf: false,
        read_only_reason: this.readOnly ? "historical_version" : null,
      },
      preview_status: this.version > 0 ? "STALE" : "READY",
      impact_status: this.version > 0 ? "up_to_date" : "none",
      pdf_status: this.version > 0 ? "STALE" : "CURRENT",
      pdf_artifact_id: null,
      pdf_artifact_version: null,
      pdf_artifact_input_hash: null,
      pdf_artifact_canonical_sha256: null,
      pdf_render_id: null,
      pdf_render_identity: null,
      pdf_error_code: null,
      pdf_error_message: null,
      pdf_url: null,
      snapshot_id: VISUAL_SNAPSHOT_ID,
    };
  }

  revisionPatch(
    revision: EditorialEnrichmentRevisionInput,
  ): EditorialVisualRevisionPatch {
    switch (revision.operation) {
      case "update_visual_caption":
        return {
          kind: "update_visual_caption",
          element_kind: revision.element_kind,
          element_key: revision.element_key,
          caption: revision.caption,
        };
      case "move_enrichment_element":
        return {
          kind: "move_enrichment_element",
          element_kind: revision.element_kind,
          element_key: revision.element_key,
          anchor_block_id: revision.anchor_block_id,
        };
      case "reorder_enrichment_elements":
        return {
          kind: "reorder_enrichment_elements",
          anchor_block_id: revision.anchor_block_id,
          order: revision.order,
        };
      case "remove_enrichment_element":
        return {
          kind: "remove_enrichment_element",
          element_kind: revision.element_kind,
          element_key: revision.element_key,
        };
      case "replace_source_figure":
        return {
          kind: "replace_source_figure",
          element_key: revision.element_key,
          figure_id: revision.figure_id,
          caption: revision.caption ?? null,
        };
      case "revise_chart":
        return {
          kind: "revise_chart",
          element_key: revision.element_key,
          chart: {
            ...revision.chart,
            caption: revision.chart.caption ?? null,
            placement: {
              kind: "after_paragraph",
              section_index: null,
              anchor_block_id: this.anchorBlockIdFor(
                revision.element_key as ElementKey,
              ),
            },
          },
        };
      case "revise_diagram":
        return {
          kind: "revise_diagram",
          element_key: revision.element_key,
          diagram: {
            ...revision.diagram,
            caption: revision.diagram.caption ?? null,
            placement: {
              kind: "after_paragraph",
              section_index: null,
              anchor_block_id: this.anchorBlockIdFor(
                revision.element_key as ElementKey,
              ),
            },
            edges: revision.diagram.edges ?? [],
            groups: revision.diagram.groups ?? [],
          },
        };
    }
  }

  operationFor(
    patch: EditorialVisualRevisionPatch,
  ): EditorialProposalOperation {
    switch (patch.kind) {
      case "revise_chart":
        return "rewrite_chart";
      case "revise_diagram":
        return "rewrite_diagram";
      case "remove_enrichment_element":
        return patch.element_kind === "figure"
          ? "delete_source_figure"
          : patch.element_kind === "table"
            ? "delete_table"
            : patch.element_kind === "diagram"
              ? "delete_diagram"
              : "delete_chart";
      default:
        return patch.kind;
    }
  }

  apply(patch: EditorialVisualRevisionPatch): void {
    if (patch.kind === "update_visual_caption") {
      const key = patch.element_key as ElementKey;
      this.captions[key] = patch.caption ?? "";
    } else if (patch.kind === "move_enrichment_element") {
      const key = patch.element_key as ElementKey;
      const number = paragraphNumber(patch.anchor_block_id);
      if (number === null) throw new Error("unexpected move anchor");
      this.placements[key] = `p${number}`;
      this.orders[key] = this.groupSize(this.placements[key]) + 10;
    } else if (patch.kind === "reorder_enrichment_elements") {
      patch.order.forEach((ref, index) => {
        this.orders[ref.key as ElementKey] = index;
      });
    } else if (patch.kind === "remove_enrichment_element") {
      this.removed.add(patch.element_key as ElementKey);
    } else if (patch.kind === "revise_chart") {
      const title = patch.chart.title;
      const caption = patch.chart.caption;
      if (typeof title === "string") this.chartTitle = title;
      if (typeof caption === "string") this.captions[CHART_KEY] = caption;
      const [firstPoint] = recordArray(patch.chart.points);
      if (typeof firstPoint?.value === "string") {
        this.chartPointOne = firstPoint.value;
      }
    } else if (patch.kind === "revise_diagram") {
      const [firstNode] = recordArray(patch.diagram.nodes);
      if (typeof firstNode?.label === "string") {
        this.diagramNodeLabel = firstNode.label;
      }
    } else if (patch.kind === "replace_source_figure") {
      if (patch.caption)
        this.captions[patch.element_key as ElementKey] = patch.caption;
    }
    this.version += 1;
  }
}

export interface VisualProposalRecord {
  id: string;
  patch: EditorialVisualRevisionPatch;
  state: EditorialProposalState;
  stateVersion: number;
}

export interface VisualStubOptions {
  /** First accept attempt answers 409 proposal_base_stale. */
  acceptConflictOnce?: boolean;
  /** Existing thread proposal shown on the chart discussion (E06/E17). */
  threadProposal?: VisualProposalRecord;
}

export interface VisualApiCapture {
  requests: string[];
  unexpectedRequests: string[];
  enrichmentRevisions: {
    occurrenceId: string;
    idempotencyKey: string | null;
    body: EditorialEnrichmentRevisionInput;
  }[];
  bridgeEntries: number;
}

function proposalView(
  record: VisualProposalRecord,
  occurrenceId: string,
): EditorialProposalRead {
  return {
    id: record.id,
    subject_id: VISUAL_SUBJECT_ID,
    production_run_id: VISUAL_RUN_ID,
    occurrence_id: occurrenceId,
    thread_id: null,
    origin: "manual_patch",
    state: record.state,
    state_version: record.stateVersion,
    operation: fixtureOperation(record.patch),
    base_artifact_id: VISUAL_ARTIFACT_ID,
    base_artifact_version: 1,
    base_canonical_sha256: VISUAL_CANONICAL_SHA,
    base_text: null,
    patch_sha256: "f".repeat(64),
    patch: {
      schema_version: "editorial-enrichment-visual-patch-v1",
      operation: fixtureOperation(record.patch),
      target: {},
      target_id: "target",
      occurrence_id: occurrenceId,
      before_hash: VISUAL_CANONICAL_SHA,
      visual_patch: record.patch,
      rationale: "Révision visuelle de test.",
    },
    diagnostics: {},
    created_at: "2026-11-22T10:00:00Z",
  };
}

function fixtureOperation(
  patch: EditorialVisualRevisionPatch,
): EditorialProposalOperation {
  switch (patch.kind) {
    case "revise_chart":
      return "rewrite_chart";
    case "revise_diagram":
      return "rewrite_diagram";
    case "remove_enrichment_element":
      return patch.element_kind === "figure"
        ? "delete_source_figure"
        : patch.element_kind === "diagram"
          ? "delete_diagram"
          : patch.element_kind === "table"
            ? "delete_table"
            : "delete_chart";
    default:
      return patch.kind;
  }
}

function acceptedResult(
  record: VisualProposalRecord,
): EditorialProposalAcceptResult {
  return {
    id: record.id,
    state: "accepted",
    state_version: record.stateVersion + 1,
    artifact_id: "11111111-1111-4111-8111-111111111111",
    artifact_version: 2,
    snapshot_id: VISUAL_SNAPSHOT_ID,
    impact_id: "22222222-2222-4222-8222-222222222222",
    impact_state: "pending",
  };
}

export function chartCorrectionProposal(): VisualProposalRecord {
  return {
    id: "proposal-chart-correction",
    patch: {
      kind: "revise_chart",
      element_key: CHART_KEY,
      chart: {
        key: CHART_KEY,
        kind: "bar",
        title: "Titre corrigé après revue",
        caption: "Légende corrigée après revue",
        placement: {
          kind: "after_paragraph",
          section_index: null,
          anchor_block_id: "paragraph-2-v0",
        },
        purpose: {
          question: "Que montrent les événements observés ?",
          available_data: "Deux catégories étayées par les sources.",
          comprehension_gain: "Compare les volumes observés.",
          scope: "Périmètre synthétique de la revue.",
          evidence_refs: [],
          knowledge_limits: "Limites synthétiques connues.",
          placement_reason: "Après le paragraphe deux.",
        },
        category_axis_label: "Catégories",
        value_axis_label: "Valeurs",
        unit: "événements",
        points: [
          {
            label: "Catégorie 1",
            series: "Série A",
            value: "4",
            evidence_refs: [
              {
                source_document_id: "source-1",
                kind: "fact",
                evidence_key: "fact-1",
              },
            ],
          },
          {
            label: "Catégorie 2",
            series: "Série A",
            value: "1",
            evidence_refs: [],
          },
        ],
      },
    },
    state: "proposed",
    stateVersion: 1,
  };
}

export async function installVisualEditorialStubs(
  page: Page,
  fixture: VisualEditorialFixture,
  options: VisualStubOptions = {},
): Promise<VisualApiCapture> {
  const capture: VisualApiCapture = {
    requests: [],
    unexpectedRequests: [],
    enrichmentRevisions: [],
    bridgeEntries: 0,
  };
  const proposals = new Map<string, VisualProposalRecord>();
  if (options.threadProposal) {
    proposals.set(options.threadProposal.id, options.threadProposal);
  }
  let acceptConflict = options.acceptConflictOnce ?? false;
  const subjectPath = `/api/subjects/${VISUAL_SUBJECT_ID}`;
  const snapshotPath = `${subjectPath}/editor/snapshot`;

  const threadFor = (occurrenceId: string): EditorialThreadRead => {
    const binding = {
      subject_id: VISUAL_SUBJECT_ID,
      production_run_id: VISUAL_RUN_ID,
      occurrence_id: occurrenceId,
      artifact_id: VISUAL_ARTIFACT_ID,
      artifact_version: fixture.version + 1,
      canonical_sha256: VISUAL_CANONICAL_SHA,
    };
    const threadProposal = options.threadProposal;
    if (threadProposal && occurrenceId.includes(`chart-${CHART_KEY}`)) {
      return {
        kind: "open",
        thread_id: VISUAL_THREAD_ID,
        origin: binding,
        current: binding,
        binding_state: "valid",
        previous_sessions: [],
        bridge_status: "ready",
        event_sequence: 1,
        timeline: [
          {
            id: "33333333-3333-4333-8333-333333333333",
            sequence: 1,
            kind: "proposal",
            actor_id: null,
            proposal_id: threadProposal.id,
            created_at: "2026-11-22T10:00:00Z",
            body: null,
          },
        ],
        etag: '"visual-thread"',
      };
    }
    return {
      kind: "unopened",
      binding,
      current: binding,
      bridge_status: "ready",
      event_sequence: 0,
      timeline: [],
      etag: '"visual-thread-empty"',
    };
  };

  await page.route("**/*", async (route: Route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    const pathname = url.pathname;
    if (url.origin !== EDITORIAL_APP_ORIGIN) {
      capture.unexpectedRequests.push(`${method} ${url.origin}${pathname}`);
      await route.abort("blockedbyclient");
      return;
    }
    if (!pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }
    capture.requests.push(`${method} ${pathname}`);
    if (
      await fulfillEditorialReadStatuses(
        route,
        method,
        pathname,
        fixture.snapshot(),
      )
    ) {
      return;
    }
    if (method === "GET" && pathname === subjectPath) {
      await route.fulfill({ json: EDITORIAL_FIXTURES.subject });
      return;
    }
    if (method === "GET" && pathname === `${subjectPath}/content`) {
      await route.fulfill({ status: 404, body: "" });
      return;
    }
    if (method === "GET" && pathname === snapshotPath) {
      await route.fulfill({ json: fixture.snapshot() });
      return;
    }
    if (
      method === "GET" &&
      /^\/api\/subjects\/[^/]+\/editor\/blocks\/[^/]+\/draft$/.test(pathname)
    ) {
      await route.fulfill({
        status: 404,
        json: { detail: { code: "draft_not_found" } },
      });
      return;
    }
    const revisionMatch =
      /^\/api\/subjects\/[^/]+\/editor\/blocks\/([^/]+)\/enrichment-revisions$/.exec(
        pathname,
      );
    if (method === "POST" && revisionMatch) {
      const occurrenceId = decodeURIComponent(revisionMatch[1] ?? "");
      const body = route
        .request()
        .postDataJSON() as EditorialEnrichmentRevisionInput;
      capture.enrichmentRevisions.push({
        occurrenceId,
        idempotencyKey: route.request().headers()["idempotency-key"] ?? null,
        body,
      });
      const patch = fixture.revisionPatch(body);
      const record: VisualProposalRecord = {
        id: `proposal-${proposals.size + 1}`,
        patch,
        state: "proposed",
        stateVersion: 1,
      };
      proposals.set(record.id, record);
      await route.fulfill({
        json: proposalView(record, occurrenceId),
      });
      return;
    }
    const acceptMatch =
      /^\/api\/subjects\/[^/]+\/editor\/threads\/([^/]+)\/proposals\/([^/]+)\/accept$/.exec(
        pathname,
      );
    if (method === "POST" && acceptMatch) {
      const occurrenceId = decodeURIComponent(acceptMatch[1] ?? "");
      const proposalId = decodeURIComponent(acceptMatch[2] ?? "");
      const record = proposals.get(proposalId);
      if (!record) {
        await route.fulfill({
          status: 404,
          json: { detail: { code: "proposal_not_found" } },
        });
        return;
      }
      if (acceptConflict) {
        acceptConflict = false;
        record.state = "stale";
        record.stateVersion += 1;
        await route.fulfill({
          status: 409,
          json: {
            detail: {
              code: "proposal_base_stale",
              message:
                "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition.",
            },
          },
        });
        return;
      }
      if (record.state === "proposed") {
        fixture.apply(record.patch);
        record.state = "accepted";
        record.stateVersion += 1;
      }
      await route.fulfill({ json: acceptedResult(record) });
      void occurrenceId;
      return;
    }
    const rejectMatch =
      /^\/api\/subjects\/[^/]+\/editor\/threads\/([^/]+)\/proposals\/([^/]+)\/reject$/.exec(
        pathname,
      );
    if (method === "POST" && rejectMatch) {
      const proposalId = decodeURIComponent(rejectMatch[2] ?? "");
      const record = proposals.get(proposalId);
      if (!record) {
        await route.fulfill({
          status: 404,
          json: { detail: { code: "proposal_not_found" } },
        });
        return;
      }
      record.state = "rejected";
      record.stateVersion += 1;
      await route.fulfill({
        json: {
          id: record.id,
          state: "rejected",
          state_version: record.stateVersion,
        },
      });
      return;
    }
    const proposalMatch =
      /^\/api\/subjects\/[^/]+\/editor\/threads\/([^/]+)\/proposals\/([^/]+)$/.exec(
        pathname,
      );
    if (method === "GET" && proposalMatch) {
      const occurrenceId = decodeURIComponent(proposalMatch[1] ?? "");
      const proposalId = decodeURIComponent(proposalMatch[2] ?? "");
      const record = proposals.get(proposalId);
      if (!record) {
        await route.fulfill({
          status: 404,
          json: { detail: { code: "proposal_not_found" } },
        });
        return;
      }
      await route.fulfill({ json: proposalView(record, occurrenceId) });
      return;
    }
    const entriesMatch =
      /^\/api\/subjects\/[^/]+\/editor\/threads\/([^/]+)\/entries$/.exec(
        pathname,
      );
    if (method === "POST" && entriesMatch) {
      capture.bridgeEntries += 1;
      const occurrenceId = decodeURIComponent(entriesMatch[1] ?? "");
      await route.fulfill({
        json: {
          kind: "accepted",
          thread_id: VISUAL_THREAD_ID,
          entry_id: "44444444-4444-4444-8444-444444444444",
          generation: "queued",
        },
      });
      void occurrenceId;
      return;
    }
    const threadMatch =
      /^\/api\/subjects\/[^/]+\/editor\/threads\/([^/]+)$/.exec(pathname);
    if (method === "GET" && threadMatch) {
      const occurrenceId = decodeURIComponent(threadMatch[1] ?? "");
      const thread = threadFor(occurrenceId);
      const etag = route.request().headers()["if-none-match"];
      if (etag === thread.etag) {
        await route.fulfill({ status: 304, headers: { ETag: thread.etag } });
        return;
      }
      await route.fulfill({
        status: 200,
        headers: { ETag: thread.etag },
        json: thread,
      });
      return;
    }
    capture.unexpectedRequests.push(`${method} ${pathname}`);
    await route.abort("blockedbyclient");
  });
  return capture;
}
