import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditorialFigureCatalogPage,
  EditorialWorkspaceBlock,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { EditorialInspector } from "./EditorialInspector";
import type { InspectorTab } from "./EditorialInspector";
import { VisualWorkspaceProvider } from "./VisualWorkspaceContext";
import type {
  VisualWorkspaceActions,
  VisualWorkspaceContextValue,
} from "./visualWorkspaceContext";

vi.mock("./EditorialConversation", () => ({
  EditorialConversation: () => <div data-testid="conversation-panel" />,
}));

vi.mock("./EditorialHistory", () => ({
  EditorialHistory: () => <div data-testid="history-panel" />,
}));

const SUBJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ANCHOR_ID = "11111111-1111-4111-8111-111111111111";
const FIGURE_A_ID = "22222222-2222-4222-8222-222222222222";
const FIGURE_B_ID = "33333333-3333-4333-8333-333333333333";
const CHART_ID = "44444444-4444-4444-8444-444444444444";
const CANDIDATE_ID = "55555555-5555-4555-8555-555555555555";

function paragraphBlock(): EditorialWorkspaceBlock {
  return {
    block_id: ANCHOR_ID,
    kind: "paragraph",
    parent_block_id: null,
    position: 0,
    text: "Paragraphe d’ancrage synthétique pour les visuels.",
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: ANCHOR_ID, legacy_target_id: null },
    before_hash: "a".repeat(64),
    allowed_operations: [
      "rewrite_paragraph",
      "insert_after",
      "create_source_figure",
    ],
    revision_status: "ready",
  };
}

function figureBlock(
  blockId: string,
  position: number,
  elementKey: string,
  caption: string,
  operations: string[],
): EditorialWorkspaceBlock {
  return {
    block_id: blockId,
    kind: "figure",
    parent_block_id: null,
    position,
    text: caption,
    evidence_refs: [],
    chart: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: "b".repeat(64),
    allowed_operations: operations,
    revision_status: "ready",
    element_kind: "figure",
    element_key: elementKey,
    caption,
    figure: {
      key: elementKey,
      catalog_figure_id: blockId,
      caption,
      source_url: "https://source.example/report",
      source: "Rapport public synthétique",
      provenance: "Figure archivée, vérifiée et conservée.",
      sha256: "c".repeat(64),
      mime_type: "image/png",
      byte_size: 1024,
      width: 800,
      height: 450,
      status: "included",
      order: 0,
      anchor_block_id: ANCHOR_ID,
      placement: "after_paragraph",
      page: 3,
      section: "Résultats",
    },
  };
}

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: SUBJECT_ID,
  production_run_id: "66666666-6666-4666-8666-666666666666",
  publication_kind: "article",
  synthesis_artifact_id: "77777777-7777-4777-8777-777777777777",
  synthesis_version: 4,
  synthesis_canonical_sha256: "d".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 1,
  blocks: [
    paragraphBlock(),
    figureBlock(
      FIGURE_A_ID,
      1,
      "adopted_source_figure_aaaa",
      "Légende de la figure remplaçable",
      [
        "replace_source_figure",
        "update_visual_caption",
        "move_enrichment_element",
        "delete_source_figure",
      ],
    ),
    {
      block_id: CHART_ID,
      kind: "chart",
      parent_block_id: null,
      position: 2,
      text: "Graphique des volumes",
      evidence_refs: [],
      chart: {
        key: "chart-1",
        kind: "bar",
        title: "Graphique des volumes",
        caption: "Volumes observés.",
        legend: [],
        category_axis_label: "Catégorie",
        value_axis_label: "Valeur",
        unit: "unité",
        points: [],
        anchor_block_id: ANCHOR_ID,
      },
      revision_target: { block_id: CHART_ID, legacy_target_id: null },
      before_hash: "e".repeat(64),
      allowed_operations: ["rewrite_chart", "delete_chart"],
      revision_status: "ready",
      element_kind: "chart",
      element_key: "chart-1",
      caption: "Volumes observés.",
    },
    figureBlock(
      FIGURE_B_ID,
      3,
      "adopted_source_figure_bbbb",
      "Légende sans remplacement",
      ["delete_source_figure"],
    ),
  ],
  enrichments: [],
  document_outline: [],
  capabilities: {
    can_discuss: false,
    can_preview_revision: false,
    manual_edit: false,
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
  snapshot_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
};

const catalog: EditorialFigureCatalogPage = {
  items: [
    {
      figure_id: CANDIDATE_ID,
      original_name: "Image candidate",
      original_caption: "Légende candidate.",
      alt_text: null,
      source: {
        source_id: "88888888-8888-4888-8888-888888888888",
        title: "Rapport public synthétique",
        url: "https://source.example/report",
      },
      page: 2,
      section: "Résultats",
      context_before: null,
      context_after: null,
      width: 800,
      height: 450,
      mime_type: "image/png",
      status: "available",
      selectable: true,
      exclusion_reason: null,
      already_used: false,
    },
  ],
  sources: [],
  total: 1,
  available_count: 1,
  offset: 0,
  limit: 30,
};

function installFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url.includes("/editor/figures?")) {
        return Promise.resolve(
          new Response(JSON.stringify(catalog), {
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      throw new Error(`Unexpected request ${url}`);
    }),
  );
}

function renderVisualsTab(
  overrides: {
    onSelectBlock?: (blockId: string) => void;
    onRequestReplace?: (elementKey: string) => void;
    onRequestRemove?: (elementKey: string) => void;
    onRequestCaption?: (elementKey: string) => void;
    onRequestMove?: (elementKey: string) => void;
    onRefresh?: () => void;
  } = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const activeTab: InspectorTab = "visuels";
  return render(
    <QueryClientProvider client={client}>
      <EditorialInspector
        subjectId={SUBJECT_ID}
        snapshot={snapshot}
        block={snapshot.blocks[0] ?? null}
        sectionTitle="Contexte"
        readOnly={false}
        isEditing={false}
        localDraftText=""
        onLocalDraftChange={vi.fn()}
        onOpenFigureGallery={vi.fn()}
        onStartEditing={vi.fn()}
        onRefresh={overrides.onRefresh ?? vi.fn()}
        onBackToDocument={vi.fn()}
        onChangeTarget={vi.fn()}
        activeTab={activeTab}
        onTabChange={vi.fn()}
        onOpenHistoryThread={vi.fn()}
        {...overrides}
      />
    </QueryClientProvider>,
  );
}

function workspaceValue(
  overrides: Partial<VisualWorkspaceContextValue> = {},
): VisualWorkspaceContextValue {
  return {
    snapshot,
    readOnly: false,
    restoreElementSelection: vi.fn(),
    anchorLabel: () => null,
    visualActions: {
      selectBlock: vi.fn(),
      requestReplace: vi.fn(),
      requestRemove: vi.fn(),
      requestCaption: vi.fn(),
      requestMove: vi.fn(),
    },
    visualMove: null,
    chooseMoveAnchor: vi.fn(),
    confirmMove: vi.fn(),
    cancelMove: vi.fn(),
    ...overrides,
  };
}

function renderVisualsTabInWorkspace(value: VisualWorkspaceContextValue) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <VisualWorkspaceProvider value={value}>
        <EditorialInspector
          subjectId={SUBJECT_ID}
          snapshot={snapshot}
          block={snapshot.blocks[0] ?? null}
          sectionTitle="Contexte"
          readOnly={false}
          isEditing={false}
          localDraftText=""
          onLocalDraftChange={vi.fn()}
          onOpenFigureGallery={vi.fn()}
          onStartEditing={vi.fn()}
          onRefresh={vi.fn()}
          onBackToDocument={vi.fn()}
          onChangeTarget={vi.fn()}
          activeTab="visuels"
          onTabChange={vi.fn()}
          onOpenHistoryThread={vi.fn()}
        />
      </VisualWorkspaceProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EditorialInspector Visuels tab", () => {
  it("lists the document visuals in order with kind, caption and position", () => {
    renderVisualsTab();

    const list = screen.getByRole("list", {
      name: "Éléments visuels du document",
    });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("Figure");
    expect(items[0]).toHaveTextContent("Légende de la figure remplaçable");
    expect(items[1]).toHaveTextContent("Graphique");
    expect(items[1]).toHaveTextContent("Graphique des volumes");
    expect(items[2]).toHaveTextContent("Figure");
    expect(items[2]).toHaveTextContent("Légende sans remplacement");
    expect(items[0]).toHaveTextContent(
      "après « Paragraphe d’ancrage synthétique pour les visuels. »",
    );
  });

  it("selects the document block from a visual entry when F3-A exposes the callback", async () => {
    const user = userEvent.setup();
    const onSelectBlock = vi.fn();
    renderVisualsTab({ onSelectBlock });

    await user.click(
      screen.getByRole("button", {
        name: "Sélectionner Figure : Légende de la figure remplaçable",
      }),
    );
    expect(onSelectBlock).toHaveBeenCalledWith(FIGURE_A_ID);
  });

  it("offers Remplacer only for figures whose snapshot declares it and opens the comparison", async () => {
    installFetch();
    const user = userEvent.setup();
    renderVisualsTab();

    const replaceButtons = screen.getAllByRole("button", {
      name: "Remplacer",
    });
    expect(replaceButtons).toHaveLength(1);
    await user.click(replaceButtons[0]!);

    const dialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    expect(
      within(dialog).getByText("Légende de la figure remplaçable"),
    ).toBeVisible();
    expect(
      await within(dialog).findByRole("radio", {
        name: "Choisir Image candidate",
      }),
    ).toBeVisible();
  });

  it("delegates the other visual actions to F3-A callbacks when provided", async () => {
    installFetch();
    const user = userEvent.setup();
    const onRequestRemove = vi.fn();
    const onRequestCaption = vi.fn();
    const onRequestMove = vi.fn();
    renderVisualsTab({ onRequestRemove, onRequestCaption, onRequestMove });

    await user.click(screen.getAllByRole("button", { name: "Retirer" })[0]!);
    expect(onRequestRemove).toHaveBeenCalledWith("adopted_source_figure_aaaa");
    await user.click(
      screen.getByRole("button", { name: "Modifier la légende" }),
    );
    expect(onRequestCaption).toHaveBeenCalledWith("adopted_source_figure_aaaa");
    await user.click(screen.getByRole("button", { name: "Déplacer" }));
    expect(onRequestMove).toHaveBeenCalledWith("adopted_source_figure_aaaa");
  });

  it("routes every Visuels action through the shared workspace context", async () => {
    installFetch();
    const user = userEvent.setup();
    const actions: VisualWorkspaceActions = {
      selectBlock: vi.fn(),
      requestReplace: vi.fn(),
      requestRemove: vi.fn(),
      requestCaption: vi.fn(),
      requestMove: vi.fn(),
    };
    renderVisualsTabInWorkspace(workspaceValue({ visualActions: actions }));

    await user.click(
      screen.getByRole("button", {
        name: "Sélectionner Figure : Légende de la figure remplaçable",
      }),
    );
    expect(actions.selectBlock).toHaveBeenCalledWith(FIGURE_A_ID);

    await user.click(screen.getByRole("button", { name: "Remplacer" }));
    expect(actions.requestReplace).toHaveBeenCalledWith(
      "adopted_source_figure_aaaa",
    );

    await user.click(screen.getAllByRole("button", { name: "Retirer" })[0]!);
    expect(actions.requestRemove).toHaveBeenCalledWith(
      "adopted_source_figure_aaaa",
    );

    await user.click(
      screen.getByRole("button", { name: "Modifier la légende" }),
    );
    expect(actions.requestCaption).toHaveBeenCalledWith(
      "adopted_source_figure_aaaa",
    );

    await user.click(screen.getByRole("button", { name: "Déplacer" }));
    expect(actions.requestMove).toHaveBeenCalledWith(
      "adopted_source_figure_aaaa",
    );
  });

  it("mounts the shared inspector move panel while a visual is moving", async () => {
    const user = userEvent.setup();
    const cancelMove = vi.fn();
    renderVisualsTabInWorkspace(
      workspaceValue({
        visualMove: {
          elementBlockId: FIGURE_A_ID,
          draftAnchorId: null,
          saving: false,
          error: null,
        },
        cancelMove,
      }),
    );

    const panel = screen.getByRole("region", {
      name: "Déplacer le visuel",
    });
    expect(panel).toBeVisible();
    expect(
      within(panel).getByRole("heading", {
        name: "Déplacer Légende de la figure remplaçable",
      }),
    ).toBeVisible();
    expect(within(panel).getByText(/Emplacement prévu :/)).toBeVisible();
    await user.click(within(panel).getByRole("button", { name: "Annuler" }));
    expect(cancelMove).toHaveBeenCalledTimes(1);
  });

  it("opens the archived image gallery and seeds the insertion from the selection", async () => {
    installFetch();
    const user = userEvent.setup();
    renderVisualsTab();

    await user.click(
      screen.getByRole("button", {
        name: "Ouvrir la galerie des images archivées",
      }),
    );
    const gallery = await screen.findByRole("dialog", {
      name: "Galerie d’images sources",
    });
    await user.click(
      await within(gallery).findByRole("checkbox", {
        name: "Sélectionner Image candidate",
      }),
    );
    await user.click(
      within(gallery).getByRole("button", {
        name: "Fermer la galerie d’images sources",
      }),
    );

    expect(screen.getByText("1 image sélectionnée")).toBeVisible();
    await user.click(
      screen.getByRole("button", {
        name: "Préparer l’intégration des images sélectionnées",
      }),
    );
    expect(
      screen.getByRole("textbox", {
        name: "Légende éditoriale pour Image candidate",
      }),
    ).toBeVisible();
  });
});
