import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditorialEvidenceReference,
  EditorialManualBarChartProposalInput,
  EditorialProposalRead,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { VisualInsertionComposer } from "./VisualInsertionComposer";

const evidence: EditorialEvidenceReference = {
  source_document_id: "11111111-1111-4111-8111-111111111111",
  kind: "fact",
  evidence_key: "a".repeat(64),
};

const anchor: EditorialWorkspaceBlock = {
  block_id: "22222222-2222-4222-8222-222222222222",
  kind: "paragraph",
  parent_block_id: null,
  position: 2,
  text: "Passage synthétique de référence.",
  evidence_refs: [
    {
      ...evidence,
      source_title: "Source synthétique",
      passage: "Valeur vérifiable décrite dans la source.",
    },
  ],
  table: null,
  chart: null,
  revision_target: {
    block_id: "22222222-2222-4222-8222-222222222222",
    legacy_target_id: null,
  },
  before_hash: "b".repeat(64),
  allowed_operations: ["insert_after", "create_bar_chart", "create_table"],
  revision_status: "ready",
};

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: "subject-1",
  production_run_id: "run-1",
  publication_kind: "article",
  synthesis_artifact_id: "synthesis-1",
  synthesis_version: 1,
  synthesis_canonical_sha256: "c".repeat(64),
  enrichment_artifact_id: "enrichment-1",
  enrichment_version: 1,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 1,
  blocks: [anchor],
  enrichments: [],
  document_outline: [],
  capabilities: {
    can_discuss: true,
    can_preview_revision: false,
    manual_edit: true,
    can_add_enrichment: true,
    can_preview_pdf: false,
  },
  preview_status: "READY",
  impact_status: "none",
  pdf_status: "CURRENT",
  snapshot_id: "33333333-3333-4333-8333-333333333333",
};

const proposal: EditorialProposalRead = {
  id: "44444444-4444-4444-8444-444444444444",
  subject_id: "subject-1",
  production_run_id: "run-1",
  occurrence_id: anchor.block_id,
  thread_id: null,
  origin: "manual_patch",
  state: "proposed",
  state_version: 1,
  operation: "create_bar_chart",
  base_artifact_id: "enrichment-1",
  base_artifact_version: 1,
  base_canonical_sha256: "d".repeat(64),
  base_text: null,
  patch_sha256: "e".repeat(64),
  patch: {
    schema_version: "editorial-patch-v1",
    operation: "create_bar_chart",
    target: { kind: "enrichment_element" },
    target_id: "target-chart-1",
    occurrence_id: anchor.block_id,
    before_hash: "f".repeat(64),
    rationale: "Graphique manuel structuré.",
    enrichment_patch: {
      charts: [
        {
          key: "chart-synthetic",
          kind: "bar",
          title: "Valeurs documentées",
          caption: null,
          category_axis_label: "Catégorie",
          value_axis_label: "Valeur",
          unit: "unités",
          points: [
            {
              label: "Premier",
              series: "Série",
              value: "4",
              evidence_handles: ["fact-1"],
            },
            {
              label: "Second",
              series: "Série",
              value: "8",
              evidence_handles: ["fact-1"],
            },
          ],
          placement: {
            kind: "after_paragraph",
            anchor_block_id: anchor.block_id,
          },
          purpose: { question: "Comparer les valeurs" },
        },
      ],
    },
    evidence_refs: [evidence],
  },
  diagnostics: {},
  created_at: "2026-11-21T12:00:00Z",
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function renderComposer(block: EditorialWorkspaceBlock = anchor) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <VisualInsertionComposer
        subjectId="subject-1"
        snapshot={snapshot}
        block={block}
        readOnly={false}
      />
    </QueryClientProvider>,
  );
}

function fillChart() {
  fireEvent.change(screen.getByRole("textbox", { name: "Consigne" }), {
    target: { value: "Comparer les valeurs rapportées." },
  });
  fireEvent.change(
    screen.getByRole("textbox", { name: "Titre du graphique" }),
    {
      target: { value: "Valeurs documentées" },
    },
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Légende" }), {
    target: { value: "Série" },
  });
  fireEvent.change(
    screen.getByRole("textbox", { name: "Intitulé des catégories" }),
    {
      target: { value: "Catégorie" },
    },
  );
  fireEvent.change(
    screen.getByRole("textbox", { name: "Intitulé des valeurs" }),
    {
      target: { value: "Valeur" },
    },
  );
  fireEvent.change(screen.getByRole("textbox", { name: "Unité" }), {
    target: { value: "unités" },
  });
  fireEvent.change(
    screen.getByRole("textbox", { name: "Libellé du point 1" }),
    {
      target: { value: "Premier" },
    },
  );
  fireEvent.change(
    screen.getByRole("spinbutton", { name: "Valeur du point 1" }),
    {
      target: { value: "4" },
    },
  );
  fireEvent.click(
    within(screen.getByRole("group", { name: "Preuves du point 1" })).getByRole(
      "checkbox",
      { name: /Source synthétique/ },
    ),
  );
  fireEvent.change(
    screen.getByRole("textbox", { name: "Libellé du point 2" }),
    {
      target: { value: "Second" },
    },
  );
  fireEvent.change(
    screen.getByRole("spinbutton", { name: "Valeur du point 2" }),
    {
      target: { value: "8" },
    },
  );
  fireEvent.click(
    within(screen.getByRole("group", { name: "Preuves du point 2" })).getByRole(
      "checkbox",
      { name: /Source synthétique/ },
    ),
  );
}

describe("VisualInsertionComposer bar chart", () => {
  it("builds a manual chart proposal only from snapshot-admitted evidence", async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url = requestUrl(input);
      if (url.endsWith("/bar-chart-proposals") && init?.method === "POST") {
        return Promise.resolve(
          new Response(JSON.stringify(proposal), { status: 201 }),
        );
      }
      if (url.endsWith(`/proposals/${proposal.id}`)) {
        return Promise.resolve(
          new Response(JSON.stringify(proposal), { status: 200 }),
        );
      }
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderComposer();
    fireEvent.click(screen.getByRole("button", { name: "Ajouter après" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Graphique" }));
    expect(screen.getByRole("textbox", { name: "Consigne" })).toHaveValue(
      "Créer un graphique à barres à partir des valeurs chiffrées de ce passage et de ses preuves associées.",
    );
    expect(screen.getByRole("textbox", { name: "Consigne" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "Proposer" })).toBeDisabled();
    expect(screen.getByText("Aperçu du graphique")).toBeVisible();
    expect(
      screen.getAllByText("Valeur vérifiable décrite dans la source."),
    ).toHaveLength(2);

    fillChart();
    expect(
      screen.getByRole("button", { name: "Ajouter un point" }),
    ).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Ajouter un point" }));
    expect(
      screen.getByRole("textbox", { name: "Libellé du point 3" }),
    ).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Supprimer le point 3" }),
    );
    expect(
      screen.queryByRole("textbox", { name: "Libellé du point 3" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Proposer" }));
    await screen.findByRole("article", { name: "Proposition éditoriale" });
    const request = fetchMock.mock.calls.find(
      ([input, init]) =>
        requestUrl(input).endsWith("/bar-chart-proposals") &&
        init?.method === "POST",
    );
    expect(request).toBeDefined();
    const rawBody = request?.[1]?.body;
    if (typeof rawBody !== "string")
      throw new Error("Expected chart request JSON text");
    const body = JSON.parse(rawBody) as EditorialManualBarChartProposalInput;
    expect(body.snapshot_id).toBe(snapshot.snapshot_id);
    expect(body.anchor_block_id).toBe(anchor.block_id);
    expect(body.chart.points).toEqual([
      {
        label: "Premier",
        series: "Série",
        value: "4",
        evidence_refs: [evidence],
      },
      {
        label: "Second",
        series: "Série",
        value: "8",
        evidence_refs: [evidence],
      },
    ]);
    expect(body.chart.purpose.evidence_refs).toEqual([evidence]);
    const idempotencyKey = new Headers(request?.[1]?.headers).get(
      "Idempotency-Key",
    );
    expect(idempotencyKey).toMatch(/^[0-9a-f-]{36}$/i);
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          init?.method === "POST" && requestUrl(input).includes("/threads/"),
      ),
    ).toBe(false);
  });

  it("gates chart creation on the server operation in the snapshot", () => {
    renderComposer({
      ...anchor,
      allowed_operations: ["insert_after", "create_table"],
    });
    fireEvent.click(screen.getByRole("button", { name: "Ajouter après" }));
    expect(screen.getByRole("menuitem", { name: "Graphique" })).toBeDisabled();
    expect(
      screen.getByText(
        "Création de graphique à barres indisponible pour ce paragraphe : l’opération n’est pas autorisée par l’instantané.",
      ),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("menuitem", { name: "Graphique" }));
    expect(
      screen.queryByRole("region", { name: "Créer un graphique à barres" }),
    ).not.toBeInTheDocument();
  });

  it("closes the chart composer with Escape and restores focus to its trigger", async () => {
    renderComposer();
    const addButton = screen.getByRole("button", { name: "Ajouter après" });
    fireEvent.click(addButton);
    fireEvent.click(screen.getByRole("menuitem", { name: "Graphique" }));
    fireEvent.keyDown(window, { key: "Escape" });

    await waitFor(() => expect(addButton).toHaveFocus());
    expect(
      screen.queryByRole("region", { name: "Créer un graphique à barres" }),
    ).not.toBeInTheDocument();
  });

  it("reuses its idempotency key when retrying the same chart draft", async () => {
    let attempts = 0;
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url = requestUrl(input);
      if (url.endsWith("/bar-chart-proposals") && init?.method === "POST") {
        attempts += 1;
        if (attempts === 1) {
          return Promise.reject(new TypeError("Network unavailable"));
        }
        return Promise.resolve(
          new Response(JSON.stringify(proposal), { status: 201 }),
        );
      }
      if (url.endsWith(`/proposals/${proposal.id}`)) {
        return Promise.resolve(
          new Response(JSON.stringify(proposal), { status: 200 }),
        );
      }
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderComposer();
    fireEvent.click(screen.getByRole("button", { name: "Ajouter après" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Graphique" }));
    fillChart();
    fireEvent.click(screen.getByRole("button", { name: "Proposer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Network unavailable",
    );
    fireEvent.click(screen.getByRole("button", { name: "Proposer" }));
    await screen.findByRole("article", { name: "Proposition éditoriale" });

    const chartCalls = fetchMock.mock.calls.filter(
      ([input, init]) =>
        requestUrl(input).endsWith("/bar-chart-proposals") &&
        init?.method === "POST",
    );
    const idempotencyKeys = chartCalls.map(([, init]) =>
      new Headers(init?.headers).get("Idempotency-Key"),
    );
    expect(chartCalls).toHaveLength(2);
    expect(idempotencyKeys[0]).toBeTruthy();
    expect(idempotencyKeys[0]).toBe(idempotencyKeys[1]);
  });

  it("offers the table composer after the server refuses insufficient chart data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>((input, init) => {
        const url = requestUrl(input);
        if (url.endsWith("/bar-chart-proposals") && init?.method === "POST") {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                detail: {
                  code: "bar_chart_insufficient_data",
                  message:
                    "Aucun graphique quantitatif vérifiable ne peut être créé à partir des preuves actuelles. Un tableau descriptif peut être proposé à la place.",
                },
              }),
              { status: 422, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        throw new Error(`Unexpected request ${url}`);
      }),
    );

    renderComposer();
    fireEvent.click(screen.getByRole("button", { name: "Ajouter après" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Graphique" }));
    fillChart();
    fireEvent.click(screen.getByRole("button", { name: "Proposer" }));

    const refusal =
      "Aucun graphique quantitatif vérifiable ne peut être créé à partir des preuves actuelles. Un tableau descriptif peut être proposé à la place.";
    expect(await screen.findByRole("alert")).toHaveTextContent(refusal);
    fireEvent.click(
      screen.getByRole("button", { name: "Proposer un tableau à la place" }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("region", { name: "Créer un tableau" }),
      ).toBeVisible(),
    );
    expect(
      screen.getByRole("textbox", { name: "Titre du tableau" }),
    ).toBeVisible();
  });
});
