import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  BootstrapPlanV1,
  BootstrapSnapshotV1,
  IOCInventoryV1,
  WorkContributionView,
} from "../api";
import {
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
      status: "pending",
      reason: null,
      reserved_vt_units: 1,
      reserved_downloads: 0,
      requires_batch_evidence: false,
      route_status: "configured",
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

function baseHandler(plan: BootstrapPlanV1) {
  return (request: RecordedRequest): Response => {
    const { url } = request;
    if (url.endsWith("/bootstrap/inventory")) return jsonResponse(inventory);
    if (url.endsWith("/bootstrap/plan")) return jsonResponse(plan);
    if (url.endsWith("/bootstrap/snapshot")) return jsonResponse(snapshot);
    if (url.endsWith("/bootstrap/run")) return jsonResponse(snapshot);
    if (url.endsWith("/contributions")) return jsonResponse(contributions);
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
});

describe("SamplesResultsView", () => {
  it("sépare les seeds des découverts et rend les résultats partiels lisibles", async () => {
    renderView(baseHandler(readyPlan));
    expect(
      screen.getByText("Chargement de l’inventaire IOC…"),
    ).toBeInTheDocument();

    expect(
      await screen.findByRole("heading", {
        name: "Seeds — déjà dans l’article",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Découverts — pivots" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Rapport fichier")).toBeInTheDocument();
    expect(
      screen.getAllByText("Résultat partiel — couverture incomplète").length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText("Incomplète — curseur cursor-2"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Champ « signature » absent du rapport."),
    ).toBeInTheDocument();
    expect(screen.getByText("vt.behaviour").closest("li")).toHaveTextContent(
      "Route de comportement non attestée.",
    );
    expect(
      screen.getAllByText("Candidat — décision G3 humaine requise").length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText(
        "Un finding importé n’est jamais automatiquement étayé : son évaluation reste une décision serveur et humaine.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Un nouvel échantillon reste candidat jusqu’à une confirmation G3 humaine ; aucune automatisation ne le promeut.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("empty_content")).toBeInTheDocument();
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
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("bootstrap_state_unavailable");
  });

  it("traite l’absence de snapshot comme un état vide, pas comme une erreur", async () => {
    renderView((request) =>
      request.url.endsWith("/bootstrap/snapshot")
        ? errorResponse(404, "bootstrap_snapshot_not_found")
        : baseHandler(readyPlan)(request),
    );
    expect(
      await screen.findByText(/Aucun rapport VT enregistré pour l’instant/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
