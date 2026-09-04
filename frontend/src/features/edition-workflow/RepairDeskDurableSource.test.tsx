/**
 * LOT 24 — the Q1 source repair debt lives on the server.
 *
 * `forcedRebuilds` is optimistic UI only. These tests unmount the whole
 * console between assertions (an F5) and require the rebuild affordance to
 * come back from the server alone.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditionRepairDetail,
  EditionRepairItem,
  EditionRepairPage,
  EditionReview,
  ReviewItem,
} from "../../api/publication";
import { ReviewConsole } from "./ReviewConsole";

const EDITION_ID = "edition-lot24";
const HASH = "a".repeat(64);

const reviewItem: ReviewItem = {
  position: 1,
  subject_id: "subject-1",
  title: "Article LOT 24",
  run_id: "run-1",
  pipeline_generation: 4,
  run_status: "ready",
  document_artifact_id: "document-1",
  document_artifact_version: 2,
  document_input_hash: HASH,
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

function sourceItem(
  overrides: Partial<EditionRepairItem> = {},
): EditionRepairItem {
  return {
    repair_key: "source-s2",
    kind: "supplemental_source_unarchived",
    position: 1,
    subject_id: "subject-1",
    article_title: "Article LOT 24",
    run_id: "run-1",
    pipeline_generation: 4,
    artifact_id: "artifact-references-1",
    artifact_version: 1,
    source_id: "S2",
    source_title: "Second rapport",
    source_url: "https://two.example/report",
    collection_id: "collection-s2",
    collection_state: "archived",
    artifact_type: null,
    preview: "https://two.example/report",
    reason_code: "supplemental_source_unarchived",
    value_sha256: "",
    payload_available: false,
    effective_action: null,
    effective_decision_id: null,
    resolved: true,
    resolution_reason: "source_archived_pending_references",
    rebuild_required: true,
    recommended_stage: "rebuild_references",
    repair_state: "archived_pending_references",
    is_publication_ioc: false,
    ...overrides,
  };
}

function detailFor(item: EditionRepairItem): EditionRepairDetail {
  return {
    repair_key: item.repair_key,
    kind: item.kind,
    source_id: item.source_id,
    source_title: item.source_title,
    source_url: item.source_url,
    publisher: "Publisher test",
    collection_id: item.collection_id,
    collection_state: item.collection_state,
    repair_state: item.repair_state,
    rebuild_required: item.rebuild_required,
    effective_decision: null,
  };
}

function serverState(item: EditionRepairItem) {
  const repairPage: EditionRepairPage = {
    summary: {
      unresolved_total: item.resolved ? 0 : 1,
      sources_to_supply: item.resolved ? 0 : 1,
      rejected_iocs_to_review: 0,
      rejected_rules_to_review: 0,
      rejected_other_artifacts: 0,
      articles_with_repairs: 1,
      articles_needing_rebuild: item.rebuild_required ? 1 : 0,
    },
    items: [item],
    articles: [
      {
        subject_id: "subject-1",
        has_pending_projection: false,
        recommended_stage: item.recommended_stage ?? "none",
        active_repair_count: item.resolved ? 0 : 1,
        resolved_since_last_build_count:
          item.resolved && item.rebuild_required ? 1 : 0,
      },
    ],
    next_cursor: null,
  };
  const review: EditionReview = {
    edition_id: EDITION_ID,
    items: [reviewItem],
    can_accept: !item.rebuild_required,
    unresolved_repair_count: item.resolved ? 0 : 1,
    pending_rebuild_count: item.rebuild_required ? 1 : 0,
  };
  return { repairPage, review };
}

function stubServer(item: EditionRepairItem) {
  const { repairPage, review } = serverState(item);
  const prepared: string[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    if (init?.method === "POST" && url.endsWith("/source")) {
      prepared.push(url);
      return Promise.resolve(
        Response.json({
          repair_key: item.repair_key,
          subject_id: "subject-1",
          collection_id: "collection-created",
          collection_state: "pending",
          source_url: item.source_url,
        }),
      );
    }
    if (url.includes("/review/repairs?")) {
      return Promise.resolve(Response.json(repairPage));
    }
    if (url.includes("/review/repairs/")) {
      return Promise.resolve(Response.json(detailFor(item)));
    }
    if (url.includes("/workbench")) {
      return Promise.resolve(
        Response.json({
          subject_id: "subject-1",
          sources: [],
          claims: [],
          indicators: [],
        }),
      );
    }
    if (url.endsWith("/review")) return Promise.resolve(Response.json(review));
    return Promise.resolve(Response.json({}));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { prepared, fetchMock };
}

/** A full page load: a brand-new QueryClient with no carried-over state. */
function mountConsole() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ReviewConsole editionId={EDITION_ID} />
    </QueryClientProvider>,
  );
  return client;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Repair Desk — dette de reconstruction durable", () => {
  it("expose le bouton reconstruire depuis le serveur et le conserve après un remount", async () => {
    stubServer(sourceItem());

    mountConsole();
    expect(
      await screen.findByRole("button", { name: "Reconstruire cet article" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Accepter la production" }),
    ).toBeDisabled();
    expect(
      screen.getByText(/doit être reconstruit avant finalisation/),
    ).toBeInTheDocument();

    // F5: everything React held in memory is gone.
    cleanup();
    mountConsole();

    expect(
      await screen.findByRole("button", { name: "Reconstruire cet article" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Accepter la production" }),
    ).toBeDisabled();
  });

  it("propose de préparer une source Q1 qui n’a aucune collection", async () => {
    const missing = sourceItem({
      collection_id: null,
      collection_state: null,
      repair_state: "collection_missing",
      resolved: false,
      resolution_reason: null,
      rebuild_required: false,
      recommended_stage: "rebuild_references",
    });
    const { prepared } = stubServer(missing);

    mountConsole();
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: /two\.example\/report/ }),
    );

    expect(await screen.findByText("source non attachée")).toBeInTheDocument();
    // No upload form while there is nothing to upload to.
    expect(
      screen.queryByRole("button", { name: "Archiver cette source" }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Préparer cette source" }),
    );

    expect(prepared).toHaveLength(1);
    expect(prepared[0]).toContain(
      `/api/editions/${EDITION_ID}/review/repairs/source-s2/source`,
    );
  });
});
