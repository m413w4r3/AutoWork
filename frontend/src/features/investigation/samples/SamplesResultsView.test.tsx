import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InvestigationView } from "../../../api/investigation";
import type {
  AgentSessionView,
  BootstrapPlanV1,
  BootstrapSnapshotV1,
  IOCInventoryV1,
  TaskPageView,
  WorkContributionView,
  WorkspaceFileListV1,
} from "../api";
import {
  bodyOf,
  createTestQueryClient,
  errorResponse,
  headersOf,
  installFetch,
  jsonResponse,
  type RecordedRequest,
} from "../api/testSupport";
import { SamplesResultsView } from "./SamplesResultsView";

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SHA_C = "c".repeat(64);
const SHA_D = "d".repeat(64);
const SHA_E = "e".repeat(64);

const inventory = {
  schema_version: "vt-bootstrap-inventory.v1",
  generated_at: "2026-11-20T10:00:00Z",
  investigation_id: "inv-1",
  subject_id: "subject-1",
  production_run_id: "run-1",
  synthesis_artifact_id: "artifact-1",
  pinned_synthesis_sha256: SHA_A,
  workspace_binding_id: "ws-1",
  workspace_synthesis_sha256: SHA_A,
  seeds: [
    {
      ioc_id: "ioc-seed-1",
      kind: "sample",
      origin: "seed",
      value: SHA_B,
      hash_type: "sha256",
      hashes: [{ hash_type: "sha256", value: SHA_B }],
      sources: [
        {
          source_document_ids: ["doc-1"],
          source_refs: ["p. 1"],
          extraction_artifact_id: null,
          extraction_artifact_version: null,
          claim_ids: ["claim-1"],
        },
      ],
      claims: ["claim-1"],
      associations: [
        {
          association_id: "assoc-1",
          sample_sha256: SHA_B,
          origin: "seed",
          qualification: "candidate",
          version: 1,
        },
      ],
      quarantined: [
        {
          sample_id: "sample-1",
          sha256: SHA_B,
          state: "quarantined",
          association_id: "assoc-1",
          qualification: "candidate",
        },
      ],
      limitations: ["Pas de rapport de comportement."],
    },
  ],
  discovered: [
    {
      ioc_id: "ioc-disc-1",
      kind: "domain",
      origin: "discovered",
      value: "evil.example",
      hash_type: null,
      hashes: [],
      sources: [],
      claims: [],
      associations: [],
      quarantined: [],
      limitations: [],
    },
  ],
  limits: {
    max_iocs: 50,
    max_sources_per_ioc: 10,
    max_claims_per_ioc: 10,
    iocs_considered: 2,
    truncated: false,
  },
  limitations: [],
} satisfies IOCInventoryV1;

const snapshot = {
  schema_version: "vt-bootstrap-snapshot.v1",
  snapshot_id: "snapshot-1",
  investigation_id: "inv-1",
  pinned_synthesis_sha256: SHA_A,
  workspace_id: "ws-1",
  generated_at: "2026-11-20T11:00:00Z",
  plan_state: "ready",
  envelope_version: 1,
  inventory_sha256: SHA_C,
  plan_sha256: SHA_D,
  iocs: [
    {
      ioc_id: "ioc-seed-1",
      kind: "sample",
      value: SHA_B,
      hash_type: "sha256",
      origin: "seed",
      status: "partial",
      reason: "partial",
      operations: [
        {
          operation_key: "op-1",
          ioc_id: "ioc-seed-1",
          tool_name: "vt.file_report",
          status: "partial",
          reason: "partial",
          invocation_id: "invocation-1",
          idempotent_replay: false,
          observation_refs: ["observation-1"],
          budget_consumed: { vt_units: 1 },
          pages: 2,
          observed_count: 14,
          pagination_complete: false,
          next_cursor: "cursor-2",
          provider_routes: ["vt.files"],
          detail_code: null,
          error_code: null,
          limitations: ["Champ « signature » absent du rapport."],
        },
      ],
      download: {
        status: "downloaded",
        association_id: "assoc-1",
        sample_id: "sample-1",
        sample_sha256: SHA_B,
        qualification: "candidate",
        hash_verified_quarantine: true,
        reason: null,
      },
      association_ids: ["assoc-1"],
      quarantined_sample_ids: ["sample-1"],
    },
  ],
  coverage: [
    {
      kind: "route_unverified",
      target: "vt.behaviour",
      detail: "Route de comportement non attestée.",
      blocking: false,
    },
  ],
  limits: {
    max_iocs: 50,
    iocs_included: 1,
    inventory_truncated: false,
    max_operations: 20,
    operations_recorded: 1,
    operations_truncated: false,
    stopped_reason: null,
  },
  limitations: ["Couverture partielle des relations."],
} satisfies BootstrapSnapshotV1;

const readyPlan = {
  schema_version: "vt-bootstrap-plan.v1",
  generated_at: "2026-11-20T10:30:00Z",
  investigation_id: "inv-1",
  state: "ready",
  envelope_version: 1,
  envelope_approval_decision_id: "decision-1",
  disclosure_tlp: "amber",
  budget: { vt_units_remaining: 10, downloads_remaining: 2 },
  operations: [
    {
      operation_key: "op-1",
      ioc_id: "ioc-seed-1",
      tool_name: "vt.file_report",
      schema_version: "1",
      arguments: {},
      status: "partial",
      reason: "partial",
      reserved_vt_units: 1,
      reserved_downloads: 0,
      requires_batch_evidence: false,
      route_status: "unverified",
      limitations: [],
    },
  ],
  coverage: [],
  limits: {
    max_operations: 20,
    operations_planned: 1,
    operations_truncated: false,
    max_relationships_per_object: 10,
  },
  limitations: [],
} satisfies BootstrapPlanV1;

const awaitingPlan = {
  ...readyPlan,
  state: "awaiting_g2",
  envelope_version: null,
  envelope_approval_decision_id: null,
} satisfies BootstrapPlanV1;

const contributions = [
  {
    id: "contribution-1",
    investigation_id: "inv-1",
    workspace_id: "ws-1",
    workspace_revision_id: "revision-1",
    file_relpath: "notes/agents/task-1/finding.md",
    file_sha256: SHA_E,
    category: "finding",
    schema_version: "work-contribution.v1",
    import_status: "imported",
    origin: "host",
    origin_actor_id: null,
    task_id: "task-1",
    lease_fence: 1,
    content_blob_id: "blob-1",
    content_sha256: SHA_E,
    observation_id: null,
    finding_id: "finding-1",
    sample_association_id: "assoc-1",
    validation_error_code: null,
    citable_as_evidence: true,
    idempotency_key: "idem-contribution-1",
    observed_at: "2026-11-20T12:00:00Z",
    created_at: "2026-11-20T12:01:00Z",
  },
  {
    id: "contribution-2",
    investigation_id: "inv-1",
    workspace_id: "ws-1",
    workspace_revision_id: "revision-2",
    file_relpath: "notes/agents/task-1/draft.md",
    file_sha256: SHA_C,
    category: "work_contribution",
    schema_version: "work-contribution.v1",
    import_status: "rejected",
    origin: "host",
    origin_actor_id: null,
    task_id: null,
    lease_fence: null,
    content_blob_id: null,
    content_sha256: null,
    observation_id: null,
    finding_id: null,
    sample_association_id: null,
    validation_error_code: "empty_content",
    citable_as_evidence: false,
    idempotency_key: "idem-contribution-2",
    observed_at: "2026-11-20T12:02:00Z",
    created_at: "2026-11-20T12:03:00Z",
  },
] satisfies WorkContributionView[];

const investigation = {
  id: "inv-1",
  subject_id: "subject-1",
  production_run_id: "run-1",
  synthesis_artifact_id: "artifact-1",
  synthesis_sha256: SHA_A,
  status: "running",
  prose_authority: "bridge_only",
  version: 1,
  stop_reason: null,
  agenda: [],
  hypotheses: [],
  findings: [
    {
      id: "finding-1",
      logical_id: "finding-logical-1",
      current_version_id: "finding-version-1",
      version: 1,
      revision_chain: [],
      kind: "new_sample",
      assessment: "qualified",
      novelty: "not_confirmed",
      statement: "La contribution demande une revue humaine.",
      scope: "Échantillon de test",
      limitations: ["Évaluation humaine requise."],
      observation_refs: ["observation-1"],
      claim_refs: ["claim-1"],
      created_at: "2026-11-20T12:00:00Z",
    },
  ],
  observations: [
    {
      id: "observation-1",
      kind: "binary_analysis",
      sha256: SHA_C,
      source: "analysis-worker",
      source_version: "1",
      observed_at: "2026-11-20T11:30:00Z",
      object_type: "binary",
      object_id: "sample-1",
      limitations: [],
      invocation_ref: null,
    },
  ],
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
} satisfies InvestigationView;

const tasks: TaskPageView = {
  items: [
    {
      id: "task-1",
      investigation_id: "inv-1",
      task_key: "sample-analysis-1",
      plan_version_id: null,
      parent_task_id: null,
      depth: 0,
      title: "Analyser le hash de l’échantillon",
      question: "Quelles observations statiques sont disponibles ?",
      objective: "Relier les résultats à l’échantillon.",
      priority: "high",
      claim_refs: ["claim-1"],
      evidence_refs: ["observation-1"],
      sample_refs: [SHA_B],
      required_capabilities: [],
      allowed_workspace_paths: [],
      execution_policy: {},
      expected_outputs: [],
      status: "completed",
      status_version: 1,
      result_verification_status: "unverified",
      blocked_reason: null,
      assigned_actor_kind: null,
      assigned_actor_id: null,
      lease_id: null,
      lease_session_id: "session-1",
      lease_fence: 1,
      lease_expires_at: null,
      heartbeat_at: null,
      handoff_ref: null,
      result_refs: [],
      event_sequence: 1,
      created_by: { actor_kind: "system", actor_id: "plan" },
      created_at: "2026-11-20T10:00:00Z",
      updated_at: "2026-11-20T12:00:00Z",
    },
  ],
  total: 1,
};

const sessions: AgentSessionView[] = [
  {
    id: "session-1",
    investigation_id: "inv-1",
    task_id: "task-1",
    adapter_kind: "codex_cli",
    state: "closed",
    state_version: 2,
    host_session_ref: null,
    handoff_id: null,
    capability_sha256: SHA_D,
    probed_at: "2026-11-20T10:00:00Z",
    task_lease_id: null,
    task_lease_fence: 1,
    checkpoint_ref: null,
    checkpoint_sha256: null,
    reconciliation: null,
    reconciliation_sha256: null,
    last_activity_at: "2026-11-20T12:00:00Z",
    opened_at: "2026-11-20T10:10:00Z",
    closed_at: "2026-11-20T12:10:00Z",
    close_reason: "completed",
    created_at: "2026-11-20T10:10:00Z",
    updated_at: "2026-11-20T12:10:00Z",
  },
];

const workspaceFiles = {
  investigation_id: "inv-1",
  workspace_id: "ws-1",
  workspace_available: true,
  files: [
    {
      path: "results/task-1/static-analysis.json",
      revision_id: "workspace-revision-1",
      sha256: SHA_D,
      size: 300,
      mode: "0440",
      policy_class: "agent_owned",
      origin: "agent",
      import_status: "applied",
      task_id: "task-1",
      observed_at: "2026-11-20T12:15:00Z",
      conflict: false,
      history: [],
    },
  ],
  next_page_token: null,
} satisfies WorkspaceFileListV1;

const sampleDetail = {
  id: "assoc-1",
  sample_sha256: SHA_B,
  origin: "vt_pivot",
  qualification: "candidate",
  related_sample_sha256: [],
  version: 1,
  changes: [],
} as const;

const qualificationResponse = {
  association: {
    ...sampleDetail,
    qualification: "confirmed",
    version: 2,
  },
  idempotent_replay: false,
} as const;

function baseHandler(plan: BootstrapPlanV1) {
  return (request: RecordedRequest): Response => {
    const { url } = request;
    if (url.endsWith("/bootstrap/inventory")) return jsonResponse(inventory);
    if (url.endsWith("/bootstrap/plan")) return jsonResponse(plan);
    if (url.endsWith("/bootstrap/snapshot")) return jsonResponse(snapshot);
    if (url.endsWith("/bootstrap/run")) return jsonResponse(snapshot);
    if (url.endsWith("/contributions")) return jsonResponse(contributions);
    if (url.endsWith("/tasks")) return jsonResponse(tasks);
    if (url.endsWith("/agent-sessions")) return jsonResponse(sessions);
    if (url.includes("/workspace/files")) return jsonResponse(workspaceFiles);
    if (url === "/api/investigations/inv-1") return jsonResponse(investigation);
    if (url.endsWith("/samples")) return jsonResponse([sampleDetail]);
    if (url.endsWith("/samples/assoc-1/qualify")) {
      return jsonResponse(qualificationResponse);
    }
    return errorResponse(404, "not_found");
  };
}

function renderView(handler: (request: RecordedRequest) => Response) {
  const recorder = installFetch(handler);
  const client = createTestQueryClient();
  render(
    <QueryClientProvider client={client}>
      <SamplesResultsView investigationId="inv-1" />
    </QueryClientProvider>,
  );
  return recorder;
}

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("SamplesResultsView", () => {
  it("recherche des hashes et IOCs sans tronquer les valeurs", async () => {
    renderView(baseHandler(readyPlan));
    const search = await screen.findByRole("searchbox", {
      name: "Rechercher un IOC ou un hash",
    });
    fireEvent.change(search, { target: { value: SHA_B } });
    const table = screen.getByRole("tabpanel", { name: "IOC et échantillons" });
    expect(within(table).getByText(SHA_B)).toBeInTheDocument();
    expect(within(table).queryByText("evil.example")).not.toBeInTheDocument();
    fireEvent.change(search, { target: { value: "evil.example" } });
    expect(within(table).getByText("evil.example")).toBeInTheDocument();
    expect(within(table).queryByText(SHA_B)).not.toBeInTheDocument();
  });

  it("filtre par type, seed/découvert, disponibilité, état G3 et source", async () => {
    renderView(baseHandler(readyPlan));
    await screen.findByRole("table", { name: /Inventaire filtré/ });
    const tablePanel = screen.getByRole("tabpanel", {
      name: "IOC et échantillons",
    });
    const type = screen.getByLabelText("Type");
    const origin = screen.getByLabelText("Statut seed ou découvert");
    const availability = screen.getByLabelText("Disponibilité");
    const qualification = screen.getByLabelText("État G3");
    const source = screen.getByLabelText("Source");
    const user = userEvent.setup();
    await user.selectOptions(type, "domain");
    expect(within(tablePanel).getByText("evil.example")).toBeInTheDocument();
    expect(within(tablePanel).queryByText(SHA_B)).not.toBeInTheDocument();
    await user.selectOptions(origin, "seed");
    expect(
      screen.getByText(
        "Aucun IOC ou échantillon ne correspond à la recherche et aux filtres.",
      ),
    ).toBeInTheDocument();
    await user.selectOptions(type, "all");
    expect(within(tablePanel).getByText(SHA_B)).toBeInTheDocument();
    await user.selectOptions(availability, "available");
    expect(within(tablePanel).getByText(SHA_B)).toBeInTheDocument();
    await user.selectOptions(qualification, "candidate");
    expect(within(tablePanel).getByText(SHA_B)).toBeInTheDocument();
    await user.selectOptions(source, "vt_pivot");
    expect(within(tablePanel).getByText(SHA_B)).toBeInTheDocument();
  });

  it("trie le tableau par valeur avec un état accessible", async () => {
    renderView(baseHandler(readyPlan));
    await screen.findByRole("table", { name: /Inventaire filtré/ });
    const table = screen.getByRole("table", { name: /Inventaire filtré/ });
    const valueHeader = screen.getByRole("columnheader", { name: /Valeur/ });
    const sortButton = within(valueHeader).getByRole("button", {
      name: /Valeur/,
    });
    const user = userEvent.setup();
    await user.click(sortButton);
    expect(valueHeader).toHaveAttribute("aria-sort", "descending");
    const values = within(table)
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.textContent ?? "");
    expect(values[0]).toContain("evil.example");
  });

  it("copie exactement la valeur complète du hash", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderView(baseHandler(readyPlan));
    await screen.findByRole("table", { name: /Inventaire filtré/ });
    fireEvent.click(
      screen.getByRole("button", { name: `Copier la valeur exacte ${SHA_B}` }),
    );
    expect(writeText).toHaveBeenCalledExactlyOnceWith(SHA_B);
    expect(await screen.findByRole("status")).toHaveTextContent(
      `Valeur copiée exactement : ${SHA_B}`,
    );
  });

  it("bascule entre les sous-vues accessibles", async () => {
    renderView(baseHandler(readyPlan));
    const objectsTab = await screen.findByRole("tab", {
      name: "IOC et échantillons",
    });
    const resultsTab = screen.getByRole("tab", {
      name: "Résultats et analyses",
    });
    expect(objectsTab).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByRole("tabpanel", { name: "IOC et échantillons" }),
    ).toBeVisible();
    const user = userEvent.setup();
    await user.click(resultsTab);
    expect(resultsTab).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByRole("tabpanel", { name: "Résultats et analyses" }),
    ).toBeVisible();
    expect(objectsTab).toHaveAttribute("aria-selected", "false");
  });

  it("ouvre le détail par hash et relie analyses, findings, tâches, sessions et fichiers", async () => {
    renderView(baseHandler(readyPlan));
    const user = userEvent.setup();
    const search = await screen.findByRole("searchbox", {
      name: "Rechercher un IOC ou un hash",
    });
    await user.type(search, SHA_B);
    await user.click(
      screen.getByRole("tab", { name: "Résultats et analyses" }),
    );
    await user.click(
      screen.getByRole("button", {
        name: `Ouvrir les résultats pour ${SHA_B}`,
      }),
    );
    const detail = screen.getByRole("region", { name: `Détail de ${SHA_B}` });
    expect(within(detail).getByText("Disponible")).toBeInTheDocument();
    expect(
      within(detail).getByRole("heading", { name: "Analyse statique" }),
    ).toBeInTheDocument();
    expect(
      within(
        within(detail).getByRole("region", { name: "Analyse statique" }),
      ).getByText(/analysis-worker/),
    ).toBeInTheDocument();
    expect(detail).toHaveTextContent(
      "La contribution demande une revue humaine.",
    );
    expect(within(detail).getByText("session-1")).toBeInTheDocument();
    expect(
      within(detail).getByText("notes/agents/task-1/finding.md"),
    ).toBeInTheDocument();
    expect(
      await within(detail).findByText("results/task-1/static-analysis.json"),
    ).toBeInTheDocument();
    const taskLink = within(detail).getByRole("link", {
      name: /Analyser le hash de l’échantillon/,
    });
    expect(taskLink).toHaveAttribute(
      "href",
      expect.stringContaining("vue=plan"),
    );
    expect(taskLink).toHaveAttribute(
      "href",
      expect.stringContaining("task=task-1"),
    );
  });

  it("retrouve disponibilité, analyses, findings et tâches en trois interactions", async () => {
    renderView(baseHandler(readyPlan));
    const user = userEvent.setup();
    let interactions = 0;
    const search = await screen.findByRole("searchbox", {
      name: "Rechercher un IOC ou un hash",
    });
    await user.type(search, SHA_B);
    interactions += 1;
    await user.click(
      screen.getByRole("tab", { name: "Résultats et analyses" }),
    );
    interactions += 1;
    await user.click(
      screen.getByRole("button", {
        name: `Ouvrir les résultats pour ${SHA_B}`,
      }),
    );
    interactions += 1;

    expect(interactions).toBeLessThanOrEqual(3);
    const detail = screen.getByRole("region", { name: `Détail de ${SHA_B}` });
    expect(within(detail).getByText("Disponible")).toBeInTheDocument();
    expect(
      within(
        within(detail).getByRole("region", { name: "Analyse statique" }),
      ).getByText(/analysis-worker/),
    ).toBeInTheDocument();
    expect(detail).toHaveTextContent(
      "La contribution demande une revue humaine.",
    );
    expect(
      within(detail).getByRole("link", {
        name: /Analyser le hash de l’échantillon/,
      }),
    ).toBeInTheDocument();
  });

  it("garde les diagnostics techniques repliés par défaut", async () => {
    renderView(baseHandler(readyPlan));
    const summary = await screen.findByText("Diagnostics techniques");
    const details = summary.closest("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    const diagnostics = within(details as HTMLElement).getByRole("region", {
      name: "Pagination et clés techniques VT",
    });
    expect(diagnostics).not.toBeVisible();
    await userEvent.setup().click(summary);
    expect(diagnostics).toBeVisible();
    expect(diagnostics).toHaveTextContent("cursor-2");
    expect(details).toHaveTextContent("Route non vérifiée");
  });

  it("signale la troncature de l’inventaire dans les diagnostics repliés", async () => {
    const handler = baseHandler(readyPlan);
    renderView((request) => {
      const response = handler(request);
      if (request.url.endsWith("/bootstrap/inventory")) {
        return jsonResponse({
          ...inventory,
          limits: { ...inventory.limits, truncated: true },
          limitations: ["Inventaire tronqué au plafond de requête."],
        });
      }
      if (request.url.endsWith("/bootstrap/snapshot")) {
        return jsonResponse({
          ...snapshot,
          limits: { ...snapshot.limits, inventory_truncated: true },
        });
      }
      return response;
    });
    const summary = await screen.findByText("Diagnostics techniques");
    const details = summary.closest("details") as HTMLElement;
    expect(details).not.toHaveAttribute("open");
    await userEvent.setup().click(summary);
    await within(details).findByText(
      "Inventaire tronqué au plafond de requête.",
    );
    expect(details).toHaveTextContent("inventaire tronqué");
    expect(details).toHaveTextContent(
      "Inventaire tronqué au plafond de requête.",
    );
    expect(details).toHaveTextContent(
      "Des IOC ou résultats peuvent manquer dans les objets affichés.",
    );
  });

  it("ne confond pas un rapport VT réussi avec la disponibilité des octets sample", async () => {
    const handler = baseHandler(readyPlan);
    renderView((request) => {
      if (request.url.endsWith("/bootstrap/inventory")) {
        return jsonResponse({
          ...inventory,
          seeds: inventory.seeds.map((ioc) => ({
            ...ioc,
            associations: [],
            quarantined: [],
          })),
        });
      }
      if (request.url.endsWith("/bootstrap/snapshot")) {
        return jsonResponse({
          ...snapshot,
          iocs: snapshot.iocs.map((ioc) => ({
            ...ioc,
            status: "succeeded",
            download: null,
          })),
        });
      }
      return handler(request);
    });
    await screen.findByRole("table", { name: /Inventaire filtré/ });
    const table = screen.getByRole("table", { name: /Inventaire filtré/ });
    const sampleRow = within(table)
      .getAllByRole("row")
      .find((row) => row.textContent?.includes(SHA_B));
    expect(sampleRow).toBeDefined();
    const user = userEvent.setup();
    await user.click(
      within(sampleRow as HTMLElement).getByRole("button", { name: "Détail" }),
    );
    const detail = screen.getByRole("region", { name: new RegExp(SHA_B) });
    const vtSection = await within(detail).findByRole("region", {
      name: "Collecte VirusTotal",
    });
    await within(vtSection).findByText(/Rapport fichier/);
    expect(vtSection).toHaveTextContent("Réussi");
    expect(
      within(sampleRow as HTMLElement).getByText("Non établie"),
    ).toBeInTheDocument();
  });

  it("n’offre pas la collecte quand le plan attend l’enveloppe G2", async () => {
    const recorder = renderView(baseHandler(awaitingPlan));
    expect(
      await screen.findByText(
        "Collecte en attente de l’approbation humaine de l’enveloppe G2.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Lancer la collecte VT" }),
    ).not.toBeInTheDocument();
    expect(
      recorder.calls.some((call) => call.url.endsWith("/bootstrap/run")),
    ).toBe(false);
  });

  it("lance la collecte au clavier avec une clé d’idempotence", async () => {
    const recorder = renderView(baseHandler(readyPlan));
    const button = await screen.findByRole("button", {
      name: "Relancer la collecte VT",
    });
    button.focus();
    expect(button).toHaveFocus();
    await userEvent.setup().keyboard("{Enter}");
    const run = recorder.calls.find((call) =>
      call.url.endsWith("/bootstrap/run"),
    );
    expect(run).toBeDefined();
    expect(run?.init?.method).toBe("POST");
    expect(headersOf(run?.init)["Idempotency-Key"]).toMatch(
      /^[A-Za-z0-9._:-]{8,128}$/,
    );
  });

  it("affiche une erreur typée quand l’inventaire est indisponible", async () => {
    renderView((request) =>
      request.url.endsWith("/bootstrap/inventory")
        ? errorResponse(503, "bootstrap_state_unavailable")
        : baseHandler(readyPlan)(request),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "bootstrap_state_unavailable",
    );
  });

  it("qualifie un échantillon en G3 avec CAS, motif et observation de l’enquête", async () => {
    const recorder = renderView(baseHandler(readyPlan));
    const user = userEvent.setup();
    await screen.findByRole("table", { name: /Inventaire filtré/ });
    await user.click(
      within(screen.getByRole("row", { name: new RegExp(SHA_B) })).getByRole(
        "button",
        { name: "Détail" },
      ),
    );
    const g3Summary = await screen.findByText(
      "Historique et décision humaine G3",
    );
    await user.click(g3Summary);
    await screen.findByRole("heading", {
      name: "Qualification G3 des échantillons",
    });
    await user.selectOptions(
      screen.getByLabelText(/Décision G3 pour/),
      "confirmed",
    );
    await user.type(
      screen.getByLabelText(/Motif de la décision/),
      "Le binaire est étayé par l’observation de l’enquête.",
    );
    await user.selectOptions(
      screen.getByLabelText(/Observations de la même enquête/),
      ["observation-1"],
    );
    await user.click(screen.getByLabelText(/Je confirme cette décision G3/));
    const submit = screen.getByRole("button", {
      name: "Enregistrer la décision G3",
    });
    expect(submit).toBeEnabled();
    await user.click(submit);

    const call = recorder.calls.find((item) =>
      item.url.endsWith("/samples/assoc-1/qualify"),
    );
    expect(call).toBeDefined();
    expect(call?.init?.method).toBe("POST");
    const payload = bodyOf(call?.init);
    expect(payload).toMatchObject({
      expected_version: 1,
      qualification: "confirmed",
      reason: "Le binaire est étayé par l’observation de l’enquête.",
      evidence_refs: ["observation-1"],
    });
    if (typeof payload !== "object" || payload === null)
      throw new Error("qualification request body expected");
    expect((payload as Record<string, unknown>).correlation_id).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    expect(headersOf(call?.init)["Idempotency-Key"]).toMatch(
      /^[A-Za-z0-9._:-]{8,128}$/,
    );
    expect(
      await screen.findByText(/Décision G3 enregistrée/),
    ).toBeInTheDocument();
  });

  it("traite l’absence de snapshot comme un état vide, pas comme une erreur", async () => {
    renderView((request) =>
      request.url.endsWith("/bootstrap/snapshot")
        ? errorResponse(404, "bootstrap_snapshot_not_found")
        : baseHandler(readyPlan)(request),
    );
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("tab", { name: "Résultats et analyses" }),
    );
    await user.click(
      await screen.findByRole("button", {
        name: `Ouvrir les résultats pour ${SHA_B}`,
      }),
    );
    const detail = screen.getByRole("region", { name: `Détail de ${SHA_B}` });
    expect(
      within(detail).getByText(
        "Aucun résultat de collecte VT enregistré pour cet objet.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
