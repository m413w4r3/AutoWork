import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  BootstrapEnvelopeApprovalV1,
  BootstrapEnvelopeProposalV1,
  BootstrapIocV1,
  BootstrapSnapshotV1,
  IOCInventoryV1,
} from "../api/bootstrap";
import type {
  WorkspaceEnsureResultV1,
  WorkspaceStatusV1,
} from "../api/workspace";
import type { WorkspaceAgentStatusResponse } from "../api/workspaceAgent";
import { OverviewView } from "./OverviewView";

const INVESTIGATION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const SUBJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const RUN_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const SYNTHESIS_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const SHA256 = "a".repeat(64);
const PROPOSAL_SHA = "b".repeat(64);
const CORRELATION_ID = "11111111-1111-4111-8111-111111111111";

const URLS = {
  agent: "/api/workspace-agent/status",
  workspace: `/api/investigations/${INVESTIGATION_ID}/workspace`,
  ensure: `/api/investigations/${INVESTIGATION_ID}/workspace/ensure`,
  inventory: `/api/investigations/${INVESTIGATION_ID}/bootstrap/inventory`,
  snapshot: `/api/investigations/${INVESTIGATION_ID}/bootstrap/snapshot`,
  proposal: `/api/investigations/${INVESTIGATION_ID}/bootstrap/envelope-proposal`,
  approve: `/api/investigations/${INVESTIGATION_ID}/bootstrap/envelope/approve`,
};

function makeAgent(
  overrides: Partial<WorkspaceAgentStatusResponse> = {},
): WorkspaceAgentStatusResponse {
  return { status: "available", instance_id: "host-agent-1", ...overrides };
}

function makeWorkspace(
  overrides: Partial<WorkspaceStatusV1> = {},
): WorkspaceStatusV1 {
  return {
    investigation_id: INVESTIGATION_ID,
    workspace_state: "bound",
    workspace_available: true,
    availability_code: "available",
    availability_reason: null,
    binding: {
      workspace_id: "ws-1",
      investigation_id: INVESTIGATION_ID,
      subject_id: SUBJECT_ID,
      delivery_code: "IR2609",
      article_slug: "article-1",
      synthesis_sha256: SHA256,
      created_at: "2026-11-22T09:00:00Z",
    },
    manifest: null,
    generation: 1,
    cursor: 2,
    counts: {
      files: 3,
      human_notes: 1,
      unresolved_conflicts: 0,
      policy_violations: 0,
      plans: 1,
      tasks: 2,
      findings: 0,
    },
    lastly_observed_at: null,
    ...overrides,
  };
}

function makeUnboundWorkspace(): WorkspaceStatusV1 {
  return makeWorkspace({
    workspace_state: "unbound",
    workspace_available: false,
    availability_code: "not_bound",
    binding: null,
    generation: null,
    cursor: null,
  });
}

function makeEnsureResult(): WorkspaceEnsureResultV1 {
  const workspace = makeWorkspace();
  if (workspace.binding === null) throw new Error("bound workspace expected");
  return {
    binding: workspace.binding,
    binding_created: true,
    workspace_created: true,
  };
}

function makeInventory(): IOCInventoryV1 {
  const ioc: BootstrapIocV1 = {
    ioc_id: "ioc-1",
    kind: "domain",
    origin: "seed",
    value: "example.invalid",
    hash_type: null,
    hashes: [],
    sources: [
      {
        source_document_ids: [],
        source_refs: ["claim-1"],
        extraction_artifact_id: null,
        extraction_artifact_version: null,
        claim_ids: ["claim-1"],
      },
    ],
    claims: ["claim-1"],
    associations: [],
    quarantined: [],
    limitations: [],
  };
  return {
    schema_version: "vt-bootstrap-inventory.v1",
    generated_at: "2026-11-22T09:30:00Z",
    investigation_id: INVESTIGATION_ID,
    subject_id: SUBJECT_ID,
    production_run_id: RUN_ID,
    synthesis_artifact_id: SYNTHESIS_ID,
    pinned_synthesis_sha256: SHA256,
    workspace_binding_id: "ws-1",
    workspace_synthesis_sha256: SHA256,
    seeds: [ioc],
    discovered: [],
    limits: {
      max_iocs: 50,
      max_sources_per_ioc: 5,
      max_claims_per_ioc: 5,
      iocs_considered: 1,
      truncated: false,
    },
    limitations: [],
  };
}

function makeSnapshot(): BootstrapSnapshotV1 {
  return {
    schema_version: "vt-bootstrap-snapshot.v1",
    snapshot_id: "99999999-9999-4999-8999-999999999999",
    investigation_id: INVESTIGATION_ID,
    pinned_synthesis_sha256: SHA256,
    workspace_id: "ws-1",
    generated_at: "2026-11-22T09:45:00Z",
    plan_state: "awaiting_g2",
    envelope_version: null,
    inventory_sha256: "c".repeat(64),
    plan_sha256: "d".repeat(64),
    iocs: [],
    coverage: [],
    limits: {
      max_iocs: 50,
      iocs_included: 0,
      inventory_truncated: false,
      max_operations: 20,
      operations_recorded: 0,
      operations_truncated: false,
      stopped_reason: null,
    },
    limitations: [],
  };
}

function makeProposal(
  overrides: Partial<BootstrapEnvelopeProposalV1> = {},
): BootstrapEnvelopeProposalV1 {
  return {
    schema_version: "vt-bootstrap-envelope-proposal.v1",
    generated_at: "2026-11-22T10:00:00Z",
    investigation_id: INVESTIGATION_ID,
    proposal_sha256: PROPOSAL_SHA,
    approved: false,
    already_approved: false,
    envelope_version: null,
    plan_state: "awaiting_g2",
    allowed_providers: ["vt"],
    acquire_hits_allowed: true,
    inventory: {
      seeds: 1,
      discovered: 0,
      samples: 1,
      ipv4: 0,
      domains: 1,
      urls: 0,
      truncated: false,
    },
    planned_operations: 4,
    covered_operations: 4,
    covered_ioc_ids: ["ioc-1"],
    vt_reads: {
      allowed: true,
      provider: "vt",
      operations_planned: 3,
      operations_covered: 3,
      units_planned: 6,
      units_proposed: 6,
      iocs_planned: 1,
      iocs_covered: 1,
      per_tool: [
        { tool_name: "vt.file_report", planned: 1, covered: 1 },
        { tool_name: "vt.object_report", planned: 1, covered: 1 },
        { tool_name: "vt.relationships", planned: 1, covered: 1 },
        { tool_name: "vt.behaviour", planned: 0, covered: 0 },
      ],
      summary: "Lectures VT bornées.",
      limitations: [],
    },
    sample_acquisition: {
      allowed: true,
      acquire_hits_allowed: true,
      sample_count_planned: 1,
      sample_count_covered: 1,
      downloads_limit: 1,
      per_sample_max_bytes: 1_048_576,
      total_max_bytes: 1_048_576,
      route_status: "configured",
      summary: "Acquisition bornée au volume proposé.",
      limitations: [],
    },
    costs: {
      limits: [
        { dimension: "vt_units", limit: 6, source: "inventory" },
        { dimension: "downloads", limit: 1, source: "settings_cap" },
      ],
      caps: {
        vt_units: 10,
        downloads: 2,
        per_sample_bytes: 1_048_576,
        tool_seconds: 120,
        context_bytes: 100_000,
        context_tokens: 20_000,
      },
      summary: "Plafonds dérivés des réglages.",
    },
    external_model: {
      allowed: false,
      providers: [],
      policy_source: "article_policy",
      summary: "Aucun modèle externe autorisé.",
      limitations: [],
    },
    disclosure: {
      tlp: "amber",
      do_not_submit: true,
      external_llm_allowed: false,
      scope: "article_cited_iocs",
      scope_statement: "IOC cités dans l’article uniquement.",
      summary: "Divulgation bornée aux IOC cités.",
      limitations: [],
    },
    summary: ["Enveloppe initiale bornée."],
    limitations: [],
    ...overrides,
  };
}

function makeApproval(): BootstrapEnvelopeApprovalV1 {
  return {
    schema_version: "vt-bootstrap-envelope-approval.v1",
    approved: true,
    investigation_id: INVESTIGATION_ID,
    proposal: makeProposal({ already_approved: true, envelope_version: 1 }),
    envelope_version: 1,
    approval_decision_id: "88888888-8888-4888-8888-888888888888",
    approval_actor_id: "human-1",
    approved_at: "2026-11-22T10:05:00Z",
    allowed_providers: ["vt"],
    acquire_hits_allowed: true,
    budget_limits: { vt_units: 6, downloads: 1 },
    disclosure_tlp: "amber",
    disclosure_do_not_submit: true,
    disclosure_external_llm_allowed: false,
  };
}

interface FetchOptions {
  agent?: WorkspaceAgentStatusResponse;
  workspace?: WorkspaceStatusV1;
  inventory?: IOCInventoryV1;
  snapshot?: BootstrapSnapshotV1;
  proposal?: BootstrapEnvelopeProposalV1 | { status: number; code: string };
  approval?: BootstrapEnvelopeApprovalV1 | { status: number; code: string };
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

function errorResponse(status: number, code: string) {
  return Response.json(
    { detail: { code, message: `Refus simulé ${code}` } },
    { status },
  );
}

function createFetch(options: FetchOptions) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = urlOf(input);
    calls.push({ url, init });
    if (url === URLS.agent)
      return Promise.resolve(Response.json(options.agent ?? makeAgent()));
    if (url === URLS.workspace)
      return Promise.resolve(
        Response.json(options.workspace ?? makeWorkspace()),
      );
    if (url === URLS.ensure && init?.method === "POST")
      return Promise.resolve(Response.json(makeEnsureResult()));
    if (url === URLS.inventory)
      return Promise.resolve(
        Response.json(options.inventory ?? makeInventory()),
      );
    if (url === URLS.snapshot)
      return Promise.resolve(Response.json(options.snapshot ?? makeSnapshot()));
    if (url === URLS.proposal && init?.method !== "POST") {
      const proposal = options.proposal ?? makeProposal();
      if ("status" in proposal)
        return Promise.resolve(errorResponse(proposal.status, proposal.code));
      return Promise.resolve(Response.json(proposal));
    }
    if (url === URLS.approve && init?.method === "POST") {
      const approval = options.approval ?? makeApproval();
      if ("status" in approval)
        return Promise.resolve(errorResponse(approval.status, approval.code));
      return Promise.resolve(Response.json(approval));
    }
    return Promise.resolve(errorResponse(500, "unexpected_request"));
  });
  return { fetchMock, calls };
}

function renderOverview(options: FetchOptions) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  vi.stubGlobal("crypto", { randomUUID: () => CORRELATION_ID });
  const fetchState = createFetch(options);
  vi.stubGlobal("fetch", fetchState.fetchMock);
  const result = render(
    <QueryClientProvider client={client}>
      <OverviewView investigationId={INVESTIGATION_ID} subjectId={SUBJECT_ID} />
    </QueryClientProvider>,
  );
  return { ...result, ...fetchState };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("OverviewView", () => {
  it("bloque l’approbation quand le workspace est absent", async () => {
    const { calls } = renderOverview({
      workspace: makeUnboundWorkspace(),
    });

    expect(await screen.findByText("Workspace local à créer")).toBeTruthy();
    const approveButton = await screen.findByRole("button", {
      name: /Approuver l’enveloppe/,
    });
    expect(approveButton).toBeDisabled();
    expect(
      screen.getByText(
        "Le workspace local doit être créé avant toute décision G2.",
      ),
    ).toBeTruthy();
    expect(calls.filter((call) => call.init?.method === "POST")).toHaveLength(
      0,
    );
  });

  it("crée le workspace local depuis la vue d’ensemble quand l’agent hôte est présent", async () => {
    const user = userEvent.setup();
    const { calls } = renderOverview({
      workspace: makeUnboundWorkspace(),
    });

    await user.click(
      await screen.findByRole("button", { name: "Créer le workspace" }),
    );

    await waitFor(() => {
      expect(
        calls.some(
          (call) => call.url === URLS.ensure && call.init?.method === "POST",
        ),
      ).toBe(true);
    });
  });

  it("ne propose pas de créer le workspace quand l’agent hôte est absent", async () => {
    renderOverview({
      agent: makeAgent({ status: "unavailable" }),
      workspace: makeUnboundWorkspace(),
    });

    expect(await screen.findByText("Workspace local absent")).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Créer le workspace" }),
    ).toBeNull();
  });

  it("envoie la requête d’approbation G2 avec l’empreinte et l’idempotence", async () => {
    const user = userEvent.setup();
    const { calls } = renderOverview({});

    await user.type(
      await screen.findByLabelText(/Motif de la décision/),
      "Approbation de l’enveloppe initiale",
    );
    await user.click(screen.getByLabelText(/Je confirme approuver/));
    const approveButton = screen.getByRole("button", {
      name: /Approuver l’enveloppe/,
    });
    expect(approveButton).toBeEnabled();
    await user.click(approveButton);

    expect(
      await screen.findByText("Enveloppe G2 approuvée — version 1"),
    ).toBeTruthy();

    const post = calls.find((call) => call.init?.method === "POST");
    if (!post) throw new Error("approval request missing");
    expect(post.url).toBe(URLS.approve);
    expect(new Headers(post.init?.headers).get("Idempotency-Key")).toBeTruthy();
    const body: unknown =
      typeof post.init?.body === "string"
        ? (JSON.parse(post.init.body) as unknown)
        : null;
    expect(body).toEqual({
      reason: "Approbation de l’enveloppe initiale",
      correlation_id: CORRELATION_ID,
      proposal_sha256: PROPOSAL_SHA,
    });
  });

  it("affiche l’état expiré avec le code typé quand la proposition est périmée", async () => {
    renderOverview({
      proposal: { status: 409, code: "bootstrap_proposal_stale" },
    });

    expect(await screen.findByText("Proposition expirée")).toBeTruthy();
    expect(screen.getByText("bootstrap_proposal_stale")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Recharger la proposition" }),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: /Approuver l’enveloppe/ }),
    ).toBeNull();
  });

  it("affiche un refus d’approbation sans masquer le code typé", async () => {
    const user = userEvent.setup();
    renderOverview({
      approval: { status: 409, code: "envelope_conflict" },
    });

    await user.type(
      await screen.findByLabelText(/Motif de la décision/),
      "Approbation demandée",
    );
    await user.click(screen.getByLabelText(/Je confirme approuver/));
    await user.click(
      screen.getByRole("button", { name: /Approuver l’enveloppe/ }),
    );

    expect(await screen.findByText("Enveloppe G2 refusée")).toBeTruthy();
    expect(screen.getByText("envelope_conflict")).toBeTruthy();
  });

  it("affiche l’état approuvé sans proposer une nouvelle décision", async () => {
    renderOverview({
      proposal: makeProposal({
        already_approved: true,
        envelope_version: 2,
      }),
    });

    expect(
      await screen.findByText("Enveloppe G2 approuvée — version 2"),
    ).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: /Approuver l’enveloppe/ }),
    ).toBeNull();
  });
});
