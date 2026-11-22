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
  EditorialManualTableProposalInput,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { createManualEditorialTableProposal } from "../../api/editorialWorkspace";
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
      passage: "Un fait vérifiable est rapporté.",
    },
  ],
  table: null,
  chart: null,
  revision_target: {
    block_id: "22222222-2222-4222-8222-222222222222",
    legacy_target_id: null,
  },
  before_hash: "b".repeat(64),
  allowed_operations: ["insert_after", "create_table"],
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
  snapshot_id: "33333333-3333-4333-8333-333333333333",
};

const proposal = {
  id: "44444444-4444-4444-8444-444444444444",
  subject_id: "subject-1",
  production_run_id: "run-1",
  occurrence_id: anchor.block_id,
  thread_id: null,
  origin: "manual_patch",
  state: "proposed",
  state_version: 1,
  operation: "create_table",
  base_artifact_id: "enrichment-1",
  base_artifact_version: 1,
  base_canonical_sha256: "d".repeat(64),
  base_text: null,
  patch_sha256: "e".repeat(64),
  patch: {
    schema_version: "editorial-patch-v1",
    operation: "create_table",
    target: { kind: "enrichment_element" },
    target_id: "target-table-1",
    occurrence_id: anchor.block_id,
    before_hash: "f".repeat(64),
    rationale: "Proposition manuelle structurée.",
    enrichment_patch: {
      tables: [
        {
          key: "table-synthetic",
          kind: "custom",
          title: "Résumé synthétique",
          caption: "Données vérifiables",
          columns: [
            { key: "item", label: "Élément" },
            { key: "value", label: "Valeur" },
          ],
          rows: [
            {
              cells: ["Échantillon", "Confirmé"],
              evidence_handles: ["evidence-1"],
            },
          ],
          placement: {
            kind: "after_paragraph",
            anchor_block_id: anchor.block_id,
          },
          purpose: {
            question: "Quelles informations sont rapportées ?",
            available_data: "Informations sélectionnées.",
            comprehension_gain: "Lecture structurée.",
            scope: "Passage courant.",
            evidence_handles: ["evidence-1"],
            knowledge_limits: "Aucune extrapolation.",
            placement_reason: "Après le passage.",
          },
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

describe("VisualInsertionComposer", () => {
  it("supports arrow navigation and Escape in the insertion menu", async () => {
    const keyboardAnchor = {
      ...anchor,
      allowed_operations: ["insert_after", "create_table", "create_bar_chart"],
    };
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });

    render(
      <QueryClientProvider client={client}>
        <VisualInsertionComposer
          subjectId="subject-1"
          snapshot={snapshot}
          block={keyboardAnchor}
          readOnly={false}
        />
      </QueryClientProvider>,
    );

    const trigger = screen.getByRole("button", { name: "Ajouter après" });
    fireEvent.click(trigger);
    const menu = screen.getByRole("menu", { name: "Ajouter après" });
    const table = within(menu).getByRole("menuitem", { name: "Tableau" });
    const chart = within(menu).getByRole("menuitem", { name: "Graphique" });

    // The authorised generation chain exposes exactly table, chart and
    // archived figures; no diagram entry is offered.
    expect(
      within(menu).getByRole("menuitem", { name: "Image issue des sources" }),
    ).toBeVisible();
    expect(within(menu).queryByRole("menuitem", { name: /Schéma/ })).toBeNull();

    expect(trigger).toHaveAttribute("aria-controls", menu.id);
    expect(table).toHaveFocus();
    fireEvent.keyDown(table, { key: "ArrowDown" });
    expect(chart).toHaveFocus();
    fireEvent.keyDown(chart, { key: "Escape" });

    await waitFor(() => {
      expect(trigger).toHaveAttribute("aria-expanded", "false");
      expect(trigger).toHaveFocus();
    });
  });

  it("turns stable table-route errors into actionable French messages", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              detail: {
                code: "manual_table_analytic_question_duplicate",
                message: "La question analytique existe déjà.",
              },
            }),
            {
              status: 422,
              headers: { "Content-Type": "application/json" },
            },
          ),
        ),
      ),
    );
    const input: EditorialManualTableProposalInput = {
      snapshot_id: snapshot.snapshot_id,
      anchor_block_id: anchor.block_id,
      table: {
        key: "table-synthetic",
        kind: "custom",
        title: "Résumé synthétique",
        columns: [
          { key: "item", label: "Élément" },
          { key: "value", label: "Valeur" },
        ],
        rows: [
          { cells: ["Échantillon", "Confirmé"], evidence_refs: [evidence] },
        ],
        purpose: {
          question: "Quelles informations sont rapportées ?",
          available_data: "Informations sélectionnées.",
          comprehension_gain: "Lecture structurée.",
          scope: "Paragraphe sélectionné.",
          evidence_refs: [evidence],
          knowledge_limits: "Aucune extrapolation.",
          placement_reason: "Après le paragraphe sélectionné.",
        },
      },
    };

    await expect(
      createManualEditorialTableProposal(
        "subject-1",
        anchor.block_id,
        "55555555-5555-4555-8555-555555555555",
        input,
      ),
    ).rejects.toMatchObject({
      code: "manual_table_analytic_question_duplicate",
      status: 422,
      message:
        "Une question analytique équivalente existe déjà. Modifiez la consigne du tableau avant de proposer une nouvelle insertion.",
    });
  });

  it("builds a typed table proposal from admitted evidence without a model call", async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url = requestUrl(input);
      if (url.endsWith("/table-proposals") && init?.method === "POST") {
        return Promise.resolve(
          new Response(JSON.stringify(proposal), {
            status: 201,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (url.endsWith(`/proposals/${proposal.id}`)) {
        return Promise.resolve(
          new Response(JSON.stringify(proposal), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onAskBridge = vi.fn();
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <VisualInsertionComposer
          subjectId="subject-1"
          snapshot={snapshot}
          block={anchor}
          readOnly={false}
          onAskBridge={onAskBridge}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Ajouter après" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Tableau" }));
    expect(screen.getByRole("textbox", { name: "Consigne" })).toHaveValue(
      "Créer un tableau à partir des informations de ce passage et de ses preuves associées.",
    );
    expect(screen.getByText("Aperçu du tableau")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Demander au Bridge" }));
    expect(onAskBridge).toHaveBeenCalledWith(
      "Propose un tableau typé à partir de ce paragraphe et de ses preuves admises, sans ajouter de fait.",
    );

    fireEvent.change(
      screen.getByRole("textbox", { name: "Titre du tableau" }),
      {
        target: { value: "Résumé synthétique" },
      },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Intitulé de colonne 1" }),
      {
        target: { value: "Élément" },
      },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Intitulé de colonne 2" }),
      {
        target: { value: "Valeur" },
      },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Cellule ligne 1, colonne 1" }),
      {
        target: { value: "Échantillon" },
      },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Cellule ligne 1, colonne 2" }),
      {
        target: { value: "Confirmé" },
      },
    );
    fireEvent.click(
      screen.getByRole("checkbox", { name: /Source synthétique/ }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Ajouter une colonne" }),
    );
    expect(
      screen.getByRole("textbox", { name: "Intitulé de colonne 3" }),
    ).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Supprimer la colonne 3" }),
    );
    expect(
      screen.queryByRole("textbox", { name: "Intitulé de colonne 3" }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Ajouter une colonne" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Ajouter une ligne" }));
    expect(
      screen.getByRole("textbox", { name: "Cellule ligne 2, colonne 1" }),
    ).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Supprimer la ligne 2" }),
    );
    expect(
      screen.queryByRole("textbox", { name: "Cellule ligne 2, colonne 1" }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Ajouter une ligne" }));

    fireEvent.change(
      screen.getByRole("textbox", { name: "Intitulé de colonne 3" }),
      {
        target: { value: "Commentaire" },
      },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Cellule ligne 1, colonne 3" }),
      {
        target: { value: "Preuve consultée" },
      },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Cellule ligne 2, colonne 1" }),
      {
        target: { value: "Second élément" },
      },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Cellule ligne 2, colonne 2" }),
      {
        target: { value: "Documenté" },
      },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Cellule ligne 2, colonne 3" }),
      {
        target: { value: "Deuxième preuve" },
      },
    );
    fireEvent.click(
      within(screen.getByRole("group", { name: "Preuves ligne 2" })).getByRole(
        "checkbox",
        { name: /Source synthétique/ },
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Proposer" }));

    await screen.findByRole("article", { name: "Proposition éditoriale" });
    const request = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(request).toBeDefined();
    const rawBody = request?.[1]?.body;
    if (typeof rawBody !== "string") {
      throw new Error("Expected the table request body to be JSON text");
    }
    const body = JSON.parse(rawBody) as {
      snapshot_id: string;
      anchor_block_id: string;
      table: {
        rows: Array<{
          cells: string[];
          evidence_refs: EditorialEvidenceReference[];
        }>;
        purpose: {
          question: string;
          evidence_refs: EditorialEvidenceReference[];
        };
      };
    };
    expect(body.snapshot_id).toBe(snapshot.snapshot_id);
    expect(body.anchor_block_id).toBe(anchor.block_id);
    expect(body.table.rows).toHaveLength(2);
    expect(body.table.rows[0]?.cells).toEqual([
      "Échantillon",
      "Confirmé",
      "Preuve consultée",
    ]);
    expect(body.table.rows[0]?.evidence_refs).toEqual([evidence]);
    expect(body.table.rows[1]?.evidence_refs).toEqual([evidence]);
    expect(body.table.purpose.evidence_refs).toEqual([evidence]);
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          init?.method === "POST" &&
          requestUrl(input).includes("/threads/") &&
          requestUrl(input).includes("/entries"),
      ),
    ).toBe(false);
  });

  it("opens the insertion flow from the real backend create operations", async () => {
    // The real snapshot emits create_table, never a generic insert_after.
    const realBackendAnchor = {
      ...anchor,
      allowed_operations: ["rewrite_paragraph", "create_table"],
    };
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    render(
      <QueryClientProvider client={client}>
        <VisualInsertionComposer
          subjectId="subject-1"
          snapshot={snapshot}
          block={realBackendAnchor}
          readOnly={false}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Ajouter après" }));
    const tableMenuItem = screen.getByRole("menuitem", { name: "Tableau" });
    expect(tableMenuItem).not.toBeDisabled();
    fireEvent.click(tableMenuItem);

    expect(
      await screen.findByRole("region", { name: "Créer un tableau" }),
    ).toBeVisible();
  });

  it("requires the server create_table capability before opening the table flow", async () => {
    const noTableOperation = {
      ...anchor,
      allowed_operations: ["insert_after"],
    };
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <VisualInsertionComposer
          subjectId="subject-1"
          snapshot={snapshot}
          block={noTableOperation}
          readOnly={false}
        />
      </QueryClientProvider>,
    );

    const addButton = screen.getByRole("button", { name: "Ajouter après" });
    fireEvent.click(addButton);
    const tableMenuItem = screen.getByRole("menuitem", { name: "Tableau" });
    expect(tableMenuItem).toBeDisabled();
    expect(
      screen.getByText(
        "Création de tableau indisponible pour ce passage : les artefacts éditoriaux ne sont pas prêts.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Demander au Bridge" }),
    ).not.toBeInTheDocument();
    fireEvent.click(tableMenuItem);
    expect(
      screen.queryByRole("region", { name: "Créer un tableau" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Proposer" })).toBeNull();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(
      screen.queryByRole("region", { name: "Créer un tableau" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(addButton).toHaveFocus());
  });

  it("reuses the proposal idempotency key after a network failure", async () => {
    const proposalRequests: RequestInit[] = [];
    let proposalAttempt = 0;
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url = requestUrl(input);
      if (url.endsWith("/table-proposals") && init?.method === "POST") {
        proposalRequests.push(init);
        proposalAttempt += 1;
        if (proposalAttempt === 1) {
          return Promise.reject(new TypeError("Network unavailable"));
        }
        if (proposalAttempt === 2) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                detail: {
                  code: "manual_table_request_invalid",
                  message: "La proposition est invalide.",
                },
              }),
              {
                status: 422,
                headers: { "Content-Type": "application/json" },
              },
            ),
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify(proposal), {
            status: 201,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      if (url.endsWith(`/proposals/${proposal.id}`)) {
        return Promise.resolve(
          new Response(JSON.stringify(proposal), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <VisualInsertionComposer
          subjectId="subject-1"
          snapshot={snapshot}
          block={anchor}
          readOnly={false}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Ajouter après" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Tableau" }));
    fireEvent.change(
      screen.getByRole("textbox", { name: "Titre du tableau" }),
      {
        target: { value: "Résumé synthétique" },
      },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Intitulé de colonne 1" }),
      { target: { value: "Élément" } },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Intitulé de colonne 2" }),
      { target: { value: "Valeur" } },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Cellule ligne 1, colonne 1" }),
      { target: { value: "Échantillon" } },
    );
    fireEvent.change(
      screen.getByRole("textbox", { name: "Cellule ligne 1, colonne 2" }),
      { target: { value: "Confirmé" } },
    );
    fireEvent.click(
      screen.getByRole("checkbox", { name: /Source synthétique/ }),
    );

    const proposeButton = screen.getByRole("button", { name: "Proposer" });
    fireEvent.click(proposeButton);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Network unavailable",
    );
    fireEvent.click(proposeButton);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Le tableau est incomplet ou incohérent.",
    );

    const keyFor = (init: RequestInit) =>
      new Headers(init.headers).get("Idempotency-Key");
    expect(proposalRequests).toHaveLength(2);
    expect(keyFor(proposalRequests[1]!)).toBe(keyFor(proposalRequests[0]!));

    fireEvent.click(proposeButton);
    await screen.findByRole("article", { name: "Proposition éditoriale" });
    expect(proposalRequests).toHaveLength(3);
    expect(keyFor(proposalRequests[2]!)).not.toBe(keyFor(proposalRequests[1]!));

    fireEvent.change(
      screen.getByRole("textbox", { name: "Titre du tableau" }),
      {
        target: { value: "Résumé synthétique révisé" },
      },
    );
    fireEvent.click(proposeButton);
    await waitFor(() => expect(proposalRequests).toHaveLength(4));
    expect(keyFor(proposalRequests[3]!)).not.toBe(keyFor(proposalRequests[2]!));
  });

  it("requires the snapshot create_source_figure operation before opening the figure flow", () => {
    const noFigureOperation = {
      ...anchor,
      allowed_operations: ["insert_after", "create_table"],
    };
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <VisualInsertionComposer
          subjectId="subject-1"
          snapshot={snapshot}
          block={noFigureOperation}
          readOnly={false}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Ajouter après" }));
    const figureMenuItem = screen.getByRole("menuitem", {
      name: "Image issue des sources",
    });
    expect(figureMenuItem).toBeDisabled();
    expect(
      screen.getByText(
        "Images sources indisponibles pour ce paragraphe : l’opération n’est pas autorisée par l’instantané.",
      ),
    ).toBeVisible();
    fireEvent.click(figureMenuItem);
    expect(
      screen.queryByRole("region", { name: "Intégrer des images sources" }),
    ).not.toBeInTheDocument();
  });

  it("renders no insertion control when can_add_enrichment is false", () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <VisualInsertionComposer
          subjectId="subject-1"
          snapshot={{
            ...snapshot,
            capabilities: {
              ...snapshot.capabilities,
              can_add_enrichment: false,
            },
          }}
          block={anchor}
          readOnly={false}
        />
      </QueryClientProvider>,
    );

    expect(
      screen.queryByRole("button", { name: "Ajouter après" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
});
