import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InvestigationView } from "../../../api/investigation";
import type { WorkspaceStatusV1 } from "../api/workspace";
import { InvestigationTab } from "../InvestigationTab";

const SUBJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const EDITION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const RUN_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const SYNTHESIS_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const INVESTIGATION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const SHA256 = "a".repeat(64);

function makeInvestigation(
  proseAuthority: InvestigationView["prose_authority"],
): InvestigationView {
  return {
    id: INVESTIGATION_ID,
    subject_id: SUBJECT_ID,
    production_run_id: RUN_ID,
    synthesis_artifact_id: SYNTHESIS_ID,
    synthesis_sha256: SHA256,
    status: "running",
    prose_authority: proseAuthority,
    version: 1,
    stop_reason: null,
    agenda: [],
    hypotheses: [],
    findings: [],
    observations: [],
    sample_associations: [],
    opinion_draft: null,
    budget: {
      bridge_turns: { limit: 8, consumed: 0, reserved: 0, remaining: 8 },
      vt_units: { limit: 10, consumed: 0, reserved: 0, remaining: 10 },
      shodan_credits: { limit: 5, consumed: 0, reserved: 0, remaining: 5 },
      downloads: { limit: 2, consumed: 0, reserved: 0, remaining: 2 },
      tool_seconds: { limit: 120, consumed: 0, reserved: 0, remaining: 120 },
      context_tokens: {
        limit: 20_000,
        consumed: 0,
        reserved: 0,
        remaining: 20_000,
      },
      context_bytes: {
        limit: 100_000,
        consumed: 0,
        reserved: 0,
        remaining: 100_000,
      },
    },
    blocked_decisions: [],
    available_tools: [],
    unavailable_tools: [],
    unavailable_tool_reasons: {},
    envelope_approved: false,
    allowed_providers: [],
    acquire_hits_allowed: false,
  };
}

function makeWorkspace(state: "bound" | "unbound"): WorkspaceStatusV1 {
  return {
    investigation_id: INVESTIGATION_ID,
    workspace_state: state,
    workspace_available: state === "bound",
    availability_code: state === "bound" ? "available" : "not_bound",
    availability_reason: null,
    binding:
      state === "bound"
        ? {
            workspace_id: "ws-1",
            investigation_id: INVESTIGATION_ID,
            subject_id: SUBJECT_ID,
            delivery_code: "IR2609",
            article_slug: "article-1",
            synthesis_sha256: SHA256,
            created_at: "2026-11-22T09:00:00Z",
          }
        : null,
    manifest: null,
    generation: state === "bound" ? 1 : null,
    cursor: state === "bound" ? 1 : null,
    counts: {
      files: 0,
      human_notes: 0,
      unresolved_conflicts: 0,
      policy_violations: 0,
      plans: 0,
      tasks: 0,
      findings: 0,
    },
    lastly_observed_at: null,
  };
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

function renderTab(
  workspaceState: "bound" | "unbound",
  proseAuthority: InvestigationView["prose_authority"],
  articleTitle = "Article étudié",
  pendingDecisionCount?: number,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  const view = makeInvestigation(proseAuthority);
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = urlOf(input);
    if (url === `/api/subjects/${SUBJECT_ID}/production/artifacts/synthesis`) {
      return Promise.resolve(
        Response.json({
          artifact_id: SYNTHESIS_ID,
          production_run_id: RUN_ID,
          stage: "synthesis",
          version: 1,
          input_hash: "b".repeat(64),
          canonical_sha256: SHA256,
          status: "verified",
          metadata: {},
          canonical_content: { schema_version: 2 },
          caption: "Synthèse vérifiée",
          provenance: "production",
        }),
      );
    }
    if (url === `/api/subjects/${SUBJECT_ID}/investigations`) {
      return Promise.resolve(
        Response.json([
          {
            id: INVESTIGATION_ID,
            subject_id: SUBJECT_ID,
            production_run_id: RUN_ID,
            synthesis_artifact_id: SYNTHESIS_ID,
            synthesis_sha256: SHA256,
            status: "running",
            version: 1,
            stop_reason: null,
            updated_at: "2026-11-22T09:00:00Z",
          },
        ]),
      );
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}/workspace`) {
      return Promise.resolve(Response.json(makeWorkspace(workspaceState)));
    }
    if (url === `/api/investigations/${INVESTIGATION_ID}`) {
      return Promise.resolve(Response.json(view));
    }
    if (url.startsWith(`/api/investigations/${INVESTIGATION_ID}/activity?`)) {
      return Promise.resolve(
        Response.json({
          schema_version: "investigation-activity.v1",
          investigation: {
            id: INVESTIGATION_ID,
            subject_id: SUBJECT_ID,
            production_run_id: RUN_ID,
            synthesis_artifact_id: SYNTHESIS_ID,
            synthesis_sha256: SHA256,
            status: "running",
            version: 1,
            stop_reason: null,
            execution_mode: "operator_led",
          },
          operator: {
            principal: null,
            model: null,
            effort: null,
            active_subagent_count: null,
            metadata_source: "unavailable_in_autowork",
          },
          working_state: null,
          last_activity_at: null,
          last_activity_source: null,
          active_backend_job_count: 0,
          budget: null,
          timeline: [],
          next_cursor: null,
          has_more: false,
          blockers: [],
          counts: { inconclusive_findings: 0 },
          generated_at: "2026-11-22T09:00:00Z",
        }),
      );
    }
    return Promise.resolve(
      Response.json(
        { detail: { code: "unexpected_request", message: "Non simulé" } },
        { status: 503 },
      ),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  const result = render(
    <QueryClientProvider client={client}>
      <InvestigationTab
        subjectId={SUBJECT_ID}
        editionId={EDITION_ID}
        articleTitle={articleTitle}
        pendingDecisionCount={pendingDecisionCount}
      />
    </QueryClientProvider>,
  );
  return { ...result, fetchMock };
}

afterEach(() => vi.unstubAllGlobals());

describe("InvestigationTab — montage du cockpit", () => {
  it("monte le cockpit M24 quand l’enquête est bridge_only, même sans workspace lié", async () => {
    const { fetchMock } = renderTab("unbound", "bridge_only");

    expect(
      await screen.findByRole("tab", { name: "Vue d’ensemble" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Article étudié" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Non lié")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Nombre de décisions non disponible"),
    ).toHaveTextContent("—");
    expect(screen.getByText("Activité")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([input]) =>
        urlOf(input).endsWith(
          `/api/investigations/${INVESTIGATION_ID}/workspace`,
        ),
      ),
    ).toHaveLength(1);
    expect(
      screen.getByRole("tablist", { name: "Vues du cockpit d’enquête" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "Laboratoire de preuve" }),
    ).toBeNull();
  });

  it("conserve le laboratoire legacy pour une enquête legacy même quand le workspace est lié", async () => {
    renderTab("bound", "legacy");

    expect(
      await screen.findByRole("region", { name: "Laboratoire de preuve" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("tablist", { name: "Vues du cockpit d’enquête" }),
    ).toBeNull();
  });

  it("affiche le compteur de décisions quand le parent le fournit", async () => {
    renderTab("bound", "bridge_only", "Article étudié", 3);

    expect(
      await screen.findByRole("tab", { name: "Vue d’ensemble" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Décisions en attente").nextElementSibling,
    ).toHaveTextContent("3");
  });
});
