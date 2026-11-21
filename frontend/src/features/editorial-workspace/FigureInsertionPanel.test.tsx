import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditorialFigureCatalogItem,
  EditorialProposalRead,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { FigureInsertionPanel } from "./FigureInsertionPanel";

const SUBJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ANCHOR_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const FIGURE_ONE = "11111111-1111-4111-8111-111111111111";
const FIGURE_TWO = "22222222-2222-4222-8222-222222222222";
const FIGURE_BLOCKED = "33333333-3333-4333-8333-333333333333";
const PROPOSAL_ID = "proposal-figures";

function catalogItem(
  figureId: string,
  name: string,
  selectable: boolean,
): EditorialFigureCatalogItem {
  return {
    figure_id: figureId,
    original_name: name,
    original_caption: `Légende ${name}`,
    alt_text: name,
    source: {
      source_id: "44444444-4444-4444-8444-444444444444",
      title: "Rapport synthétique",
      url: "https://source.example/report",
    },
    page: 3,
    section: "Résultats",
    context_before: "Contexte précédent synthétique.",
    context_after: "Contexte suivant synthétique.",
    width: 800,
    height: 450,
    mime_type: "image/png",
    status: selectable ? "available" : "ineligible",
    selectable,
    exclusion_reason: selectable
      ? null
      : "La licence ne permet pas l’intégration.",
    already_used: false,
  };
}

const items = [
  catalogItem(FIGURE_ONE, "Carte des échanges", true),
  catalogItem(FIGURE_TWO, "Schéma du réseau", true),
  catalogItem(FIGURE_BLOCKED, "Image non admissible", false),
];

function workspaceSnapshot(
  publicationKind: "article" | "brief" = "article",
  integratedFigures = 0,
): EditorialWorkspaceSnapshot {
  const anchor: EditorialWorkspaceBlock = {
    block_id: ANCHOR_ID,
    kind: "paragraph",
    parent_block_id: null,
    position: 0,
    text: "Paragraphe d’ancrage synthétique.",
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: ANCHOR_ID, legacy_target_id: null },
    before_hash: "a".repeat(64),
    allowed_operations: ["insert_after", "create_source_figure"],
    revision_status: "ready",
  };
  const figures: EditorialWorkspaceBlock[] = Array.from(
    { length: integratedFigures },
    (_, index) => ({
      ...anchor,
      block_id: `integrated-${index}`,
      kind: "figure",
      position: index + 1,
      text: `Figure déjà intégrée ${index + 1}`,
      allowed_operations: [],
      figure: {
        key: `adopted_source_figure_${FIGURE_ONE.replaceAll("-", "")}`,
        caption: `Figure déjà intégrée ${index + 1}`,
        source_url: "https://source.example/report",
        source: "Rapport synthétique",
        provenance: "Figure archivée, vérifiée et conservée.",
        sha256: "b".repeat(64),
        mime_type: "image/png",
        byte_size: 1024,
        width: 800,
        height: 450,
        status: "included",
        order: index,
        anchor_block_id: ANCHOR_ID,
        placement: "after_paragraph",
        page: 3,
        section: "Résultats",
      },
    }),
  );
  return {
    subject_id: SUBJECT_ID,
    production_run_id: "55555555-5555-4555-8555-555555555555",
    publication_kind: publicationKind,
    synthesis_artifact_id: "66666666-6666-4666-8666-666666666666",
    synthesis_version: 4,
    synthesis_canonical_sha256: "c".repeat(64),
    enrichment_artifact_id: "77777777-7777-4777-8777-777777777777",
    enrichment_version: 2,
    publication_artifact_id: null,
    publication_version: null,
    pipeline_generation: 1,
    blocks: [anchor, ...figures],
    enrichments: figures,
    document_outline: [],
    capabilities: {
      can_discuss: false,
      can_preview_revision: false,
      manual_edit: false,
      can_add_enrichment: true,
      can_preview_pdf: false,
    },
    preview_status: "READY",
    impact_status: "none",
    pdf_status: "CURRENT",
    snapshot_id: "88888888-8888-4888-8888-888888888888",
  };
}

const proposal: EditorialProposalRead = {
  id: PROPOSAL_ID,
  subject_id: SUBJECT_ID,
  production_run_id: "55555555-5555-4555-8555-555555555555",
  occurrence_id: ANCHOR_ID,
  thread_id: null,
  origin: "manual_patch",
  state: "proposed",
  state_version: 1,
  operation: "create_source_figure",
  base_artifact_id: "66666666-6666-4666-8666-666666666666",
  base_artifact_version: 4,
  base_canonical_sha256: "c".repeat(64),
  base_text: null,
  patch_sha256: "d".repeat(64),
  patch: {
    schema_version: "editorial-patch-v1",
    operation: "create_source_figure",
    target: {},
    target_id: "target-1",
    occurrence_id: ANCHOR_ID,
    before_hash: "e".repeat(64),
    rationale: "Figures archivées sélectionnées manuellement.",
    enrichment_patch: {
      manual_source_figures: [
        {
          figure_id: FIGURE_ONE,
          caption: "Légende éditoriale une",
          order: 0,
          sha256: "f".repeat(64),
          mime_type: "image/png",
          byte_size: 1024,
          width: 800,
          height: 450,
          source_document_id: "44444444-4444-4444-8444-444444444444",
          source_url: "https://source.example/report",
          provenance: "Figure archivée et validée dans le rapport.",
        },
        {
          figure_id: FIGURE_TWO,
          caption: "Légende éditoriale deux",
          order: 1,
          sha256: "0".repeat(64),
          mime_type: "image/png",
          byte_size: 2048,
          width: 1200,
          height: 800,
          source_document_id: "44444444-4444-4444-8444-444444444444",
          source_url: "https://source.example/report",
          provenance: "Figure archivée et validée dans le rapport.",
        },
      ],
    },
  },
  diagnostics: {},
  created_at: "2026-11-21T08:00:00Z",
};

function renderPanel(snapshot = workspaceSnapshot(), onRefresh = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return {
    onRefresh,
    ...render(
      <QueryClientProvider client={queryClient}>
        <FigureInsertionPanel
          subjectId={SUBJECT_ID}
          snapshot={snapshot}
          block={snapshot.blocks[0]!}
          canCreateFigure={true}
          onClose={vi.fn()}
          onRefresh={onRefresh}
        />
      </QueryClientProvider>,
    ),
  };
}

function response(json: unknown, status = 200): Response {
  return new Response(JSON.stringify(json), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("FigureInsertionPanel", () => {
  it("selects multiple eligible figures, edits their captions and order, and posts only references", async () => {
    const user = userEvent.setup();
    let submittedPayload: unknown;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (
        url.includes("/editor/figures?") &&
        (!init?.method || init.method === "GET")
      ) {
        return Promise.resolve(
          response({
            items,
            sources: [],
            total: items.length,
            available_count: 2,
            offset: 0,
            limit: 30,
          }),
        );
      }
      if (url.endsWith("/figure-proposals") && init?.method === "POST") {
        if (typeof init.body !== "string") {
          throw new Error("Expected a JSON figure proposal request body");
        }
        submittedPayload = JSON.parse(init.body) as unknown;
        return Promise.resolve(response({ ...proposal }));
      }
      if (url.endsWith(`/proposals/${PROPOSAL_ID}`)) {
        return Promise.resolve(response(proposal));
      }
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPanel();

    const gallery = await screen.findByRole("dialog", {
      name: "Galerie d’images sources",
    });
    const first = await within(gallery).findByRole("checkbox", {
      name: "Sélectionner Carte des échanges",
    });
    const second = await within(gallery).findByRole("checkbox", {
      name: "Sélectionner Schéma du réseau",
    });
    const ineligible = await within(gallery).findByRole("checkbox", {
      name: "Sélectionner Image non admissible",
    });
    expect(ineligible).toBeDisabled();

    await user.click(first);
    await user.click(second);
    expect(within(gallery).getByText("2 images sélectionnées")).toBeVisible();
    await user.click(
      within(gallery).getByRole("button", {
        name: "Fermer la galerie d’images sources",
      }),
    );

    const firstCaption = screen.getByRole("textbox", {
      name: "Légende éditoriale pour Carte des échanges",
    });
    const secondCaption = screen.getByRole("textbox", {
      name: "Légende éditoriale pour Schéma du réseau",
    });
    await user.clear(firstCaption);
    await user.type(firstCaption, "Légende éditoriale une");
    await user.clear(secondCaption);
    await user.type(secondCaption, "Légende éditoriale deux");
    await user.click(
      screen.getByRole("button", { name: "Monter Schéma du réseau" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Intégrer les images sélectionnées" }),
    );

    await screen.findByRole("heading", {
      name: "Comparaison de l’insertion de figures",
    });
    expect(submittedPayload).toEqual({
      snapshot_id: "88888888-8888-4888-8888-888888888888",
      anchor_block_id: ANCHOR_ID,
      figures: [
        { figure_id: FIGURE_TWO, caption: "Légende éditoriale deux", order: 0 },
        { figure_id: FIGURE_ONE, caption: "Légende éditoriale une", order: 1 },
      ],
    });
    expect(JSON.stringify(submittedPayload)).not.toContain("source_url");
    expect(JSON.stringify(submittedPayload)).not.toContain("sha256");
    expect(
      screen.getByText("Figures archivées sélectionnées manuellement."),
    ).toBeVisible();
  });

  it("disables figure selection once a BRIEF already contains three figures", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          response({
            items,
            sources: [],
            total: items.length,
            available_count: 2,
            offset: 0,
            limit: 30,
          }),
        ),
      ),
    );
    renderPanel(workspaceSnapshot("brief", 3));

    const gallery = await screen.findByRole("dialog", {
      name: "Galerie d’images sources",
    });
    expect(
      await within(gallery).findByRole("checkbox", {
        name: "Sélectionner Carte des échanges",
      }),
    ).toBeDisabled();
    expect(
      within(gallery).getByText(
        /Une brève ne peut contenir plus de trois figures intégrées\./,
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Intégrer les images sélectionnées" }),
    ).toBeDisabled();
  });

  it("shows the exact BRIEF limit refusal and offers to refresh the snapshot", async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        if (url.includes("/editor/figures?")) {
          return Promise.resolve(
            response({
              items,
              sources: [],
              total: items.length,
              available_count: 2,
              offset: 0,
              limit: 30,
            }),
          );
        }
        if (url.endsWith("/figure-proposals") && init?.method === "POST") {
          return Promise.resolve(
            response(
              {
                detail: {
                  code: "brief_figure_limit_exceeded",
                  message:
                    "Une brève ne peut contenir plus de trois figures intégrées.",
                },
              },
              409,
            ),
          );
        }
        throw new Error(`Unexpected request ${url}`);
      }),
    );
    renderPanel(workspaceSnapshot("brief", 2), onRefresh);

    const gallery = await screen.findByRole("dialog", {
      name: "Galerie d’images sources",
    });
    await user.click(
      await within(gallery).findByRole("checkbox", {
        name: "Sélectionner Carte des échanges",
      }),
    );
    await user.click(
      within(gallery).getByRole("button", {
        name: "Fermer la galerie d’images sources",
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "Intégrer les images sélectionnées" }),
    );

    expect(
      await screen.findByText(
        "Une brève ne peut contenir plus de trois figures intégrées.",
      ),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Actualiser l’atelier" }),
    );
    await waitFor(() => expect(onRefresh).toHaveBeenCalledOnce());
  });
});
