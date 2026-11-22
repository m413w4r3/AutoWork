import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditorialFigureCatalogItem,
  EditorialFigureCatalogPage,
  EditorialVisualRevisionProposalRead,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { ReplaceFigureDialog } from "./ReplaceFigureDialog";
import { resolveReplaceFigureTarget } from "./replaceFigureContract";

const SUBJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const ANCHOR_ID = "11111111-1111-4111-8111-111111111111";
const FIGURE_BLOCK_ID = "22222222-2222-4222-8222-222222222222";
const ELEMENT_KEY = "adopted_source_figure_bbbb";
const OLD_FIGURE_ID = "33333333-3333-4333-8333-333333333333";
const NEW_FIGURE_ID = "44444444-4444-4444-8444-444444444444";
const INELIGIBLE_ID = "55555555-5555-4555-8555-555555555555";
const PROPOSAL_ID = "66666666-6666-4666-8666-666666666666";

const anchorText = "Paragraphe d’ancrage synthétique pour la figure.";

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: SUBJECT_ID,
  production_run_id: "77777777-7777-4777-8777-777777777777",
  publication_kind: "article",
  synthesis_artifact_id: "88888888-8888-4888-8888-888888888888",
  synthesis_version: 4,
  synthesis_canonical_sha256: "a".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 1,
  blocks: [
    {
      block_id: ANCHOR_ID,
      kind: "paragraph",
      parent_block_id: null,
      position: 0,
      text: anchorText,
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: ANCHOR_ID, legacy_target_id: null },
      before_hash: "b".repeat(64),
      allowed_operations: ["rewrite_paragraph"],
      revision_status: "ready",
    },
    {
      block_id: FIGURE_BLOCK_ID,
      kind: "figure",
      parent_block_id: null,
      position: 1,
      text: "Légende actuelle de la figure",
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: FIGURE_BLOCK_ID, legacy_target_id: null },
      before_hash: "c".repeat(64),
      allowed_operations: [
        "replace_source_figure",
        "update_visual_caption",
        "move_enrichment_element",
        "delete_source_figure",
      ],
      revision_status: "ready",
      element_kind: "figure",
      element_key: ELEMENT_KEY,
      caption: "Légende actuelle de la figure",
      figure: {
        key: ELEMENT_KEY,
        catalog_figure_id: OLD_FIGURE_ID,
        caption: "Légende actuelle de la figure",
        source_url: "https://source.example/report",
        source: "Rapport public synthétique",
        provenance: "Figure archivée, vérifiée et conservée.",
        sha256: "d".repeat(64),
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
    },
  ],
  enrichments: [],
  document_outline: [],
  capabilities: {
    can_discuss: false,
    can_preview_revision: false,
    manual_edit: false,
    can_add_enrichment: false,
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

function catalogItem(
  figureId: string,
  name: string,
  status: EditorialFigureCatalogItem["status"],
  selectable: boolean,
  exclusionReason: string | null = null,
): EditorialFigureCatalogItem {
  return {
    figure_id: figureId,
    original_name: name,
    original_caption: `Légende d’origine ${name}`,
    alt_text: name,
    source: {
      source_id: "99999999-9999-4999-8999-999999999999",
      title: "Rapport public synthétique",
      url: "https://source.example/report",
    },
    page: 4,
    section: "Résultats",
    context_before: null,
    context_after: null,
    width: 800,
    height: 450,
    mime_type: "image/png",
    status,
    selectable,
    exclusion_reason: exclusionReason,
    already_used: status === "already_integrated",
  };
}

const catalog: EditorialFigureCatalogPage = {
  items: [
    catalogItem(OLD_FIGURE_ID, "Figure actuelle", "already_integrated", false),
    catalogItem(NEW_FIGURE_ID, "Carte de remplacement", "available", true),
    catalogItem(
      INELIGIBLE_ID,
      "Figure révoquée",
      "ineligible",
      false,
      "La licence de cette figure a été révoquée.",
    ),
  ],
  sources: [],
  total: 3,
  available_count: 1,
  offset: 0,
  limit: 30,
};

function proposalRead(): EditorialVisualRevisionProposalRead {
  return {
    id: PROPOSAL_ID,
    subject_id: SUBJECT_ID,
    production_run_id: snapshot.production_run_id,
    occurrence_id: FIGURE_BLOCK_ID,
    thread_id: null,
    origin: "manual_patch",
    state: "proposed",
    state_version: 1,
    operation: "replace_source_figure",
    base_artifact_id: snapshot.synthesis_artifact_id,
    base_artifact_version: snapshot.synthesis_version,
    base_canonical_sha256: snapshot.synthesis_canonical_sha256,
    base_text: null,
    patch_sha256: "f".repeat(64),
    patch: null,
    diagnostics: {},
    created_at: "2026-11-22T09:00:00Z",
  };
}

const accepted = {
  id: PROPOSAL_ID,
  state: "accepted" as const,
  state_version: 2,
  artifact_id: "artifact-accepted",
  artifact_version: 5,
  snapshot_id: "snapshot-accepted",
  impact_id: "impact-accepted",
  impact_state: "pending" as const,
};

interface CapturedRequests {
  prepareBodies: unknown[];
  acceptBodies: unknown[];
  paths: string[];
}

function installFetch(
  prepareResponse: () => Response = () => response(proposalRead()),
  acceptResponse: () => Response = () => response(accepted),
): CapturedRequests {
  const captured: CapturedRequests = {
    prepareBodies: [],
    acceptBodies: [],
    paths: [],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const method = init?.method ?? "GET";
      captured.paths.push(`${method} ${url}`);
      if (url.includes("/editor/figures?") && method === "GET") {
        return Promise.resolve(response(catalog));
      }
      if (url.endsWith("/enrichment-revisions") && method === "POST") {
        if (typeof init?.body !== "string") {
          throw new Error("Expected a JSON replacement body");
        }
        captured.prepareBodies.push(JSON.parse(init.body) as unknown);
        return Promise.resolve(prepareResponse());
      }
      if (url.endsWith("/accept") && method === "POST") {
        if (typeof init?.body !== "string") {
          throw new Error("Expected a JSON accept body");
        }
        captured.acceptBodies.push(JSON.parse(init.body) as unknown);
        return Promise.resolve(acceptResponse());
      }
      throw new Error(`Unexpected request ${method} ${url}`);
    }),
  );
  return captured;
}

function response(json: unknown, status = 200): Response {
  return new Response(JSON.stringify(json), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderDialog(
  options: {
    onClose?: () => void;
    onAccepted?: (elementKey: string) => void;
    onRefresh?: () => void;
    snapshot?: EditorialWorkspaceSnapshot;
  } = {},
) {
  const activeSnapshot = options.snapshot ?? snapshot;
  const target = resolveReplaceFigureTarget(activeSnapshot, ELEMENT_KEY);
  if (!target) throw new Error("Synthetic replacement target must resolve");
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ReplaceFigureDialog
        subjectId={SUBJECT_ID}
        snapshot={activeSnapshot}
        target={target}
        onClose={options.onClose ?? vi.fn()}
        onAccepted={options.onAccepted}
        onRefresh={options.onRefresh}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ReplaceFigureDialog", () => {
  it("compares the current and replacement images, then accepts exactly one typed revision", async () => {
    const user = userEvent.setup();
    const captured = installFetch();
    const onAccepted = vi.fn();
    renderDialog({ onAccepted });

    const dialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    expect(
      within(dialog).getByText("Légende actuelle de la figure"),
    ).toBeVisible();
    expect(
      await within(dialog).findByRole("radio", {
        name: "Choisir Carte de remplacement",
      }),
    ).toBeVisible();

    await user.click(
      within(dialog).getByRole("radio", {
        name: "Choisir Carte de remplacement",
      }),
    );

    const comparison = within(dialog).getByRole("region", {
      name: "Comparaison des images",
    });
    expect(
      within(comparison).getByRole("heading", { name: "Image actuelle" }),
    ).toBeVisible();
    expect(
      within(comparison).getByRole("heading", { name: "Nouvelle image" }),
    ).toBeVisible();
    expect(
      within(comparison).getByText("Légende actuelle de la figure"),
    ).toBeVisible();
    expect(
      within(comparison).getByText("Légende d’origine Carte de remplacement"),
    ).toBeVisible();
    expect(
      within(comparison).getByText(
        `Emplacement conservé · après « ${anchorText} »`,
      ),
    ).toBeVisible();
    expect(
      within(comparison).getByText(
        "L’archive d’origine n’est pas modifiée : la figure remplacée reste conservée dans l’inventaire.",
      ),
    ).toBeVisible();

    await user.click(
      within(comparison).getByRole("button", {
        name: "Remplacer l’image",
      }),
    );

    expect(
      await within(dialog).findByText(
        "Image remplacée. Le document contient toujours exactement une image à cet emplacement.",
      ),
    ).toBeVisible();
    expect(captured.prepareBodies).toEqual([
      {
        snapshot_id: snapshot.snapshot_id,
        operation: "replace_source_figure",
        element_key: ELEMENT_KEY,
        figure_id: NEW_FIGURE_ID,
      },
    ]);
    expect(captured.acceptBodies).toEqual([
      { expected_state_version: 1, patch_sha256: "f".repeat(64) },
    ]);
    expect(
      captured.paths.filter((path) => path.includes("figure-proposals")),
    ).toEqual([]);
    expect(
      captured.paths.filter((path) => path.includes("/enrichment-revisions")),
    ).toHaveLength(1);
    expect(onAccepted).toHaveBeenCalledWith(ELEMENT_KEY);
  });

  it("sends a modified caption only, and refuses an empty caption without a request", async () => {
    const user = userEvent.setup();
    const captured = installFetch();
    renderDialog();

    const dialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    await user.click(
      await within(dialog).findByRole("radio", {
        name: "Choisir Carte de remplacement",
      }),
    );
    const caption = within(dialog).getByRole("textbox", {
      name: "Légende de la nouvelle image",
    });
    await user.clear(caption);
    expect(
      within(dialog).getByText(
        "La légende doit contenir au moins un caractère.",
      ),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "Remplacer l’image" }),
    ).toBeDisabled();

    await user.type(caption, "Nouvelle légende éditoriale");
    await user.click(
      within(dialog).getByRole("button", { name: "Remplacer l’image" }),
    );
    await within(dialog).findByText(
      "Image remplacée. Le document contient toujours exactement une image à cet emplacement.",
    );
    expect(captured.prepareBodies).toEqual([
      {
        snapshot_id: snapshot.snapshot_id,
        operation: "replace_source_figure",
        element_key: ELEMENT_KEY,
        figure_id: NEW_FIGURE_ID,
        caption: "Nouvelle légende éditoriale",
      },
    ]);
  });

  it("keeps ineligible images unselectable with their reason", async () => {
    installFetch();
    renderDialog();

    const dialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    expect(
      await within(dialog).findByRole("radio", {
        name: "Choisir Figure révoquée",
      }),
    ).toBeDisabled();
    expect(
      within(dialog).getByText("La licence de cette figure a été révoquée."),
    ).toBeVisible();
    expect(
      within(dialog).getByText("Cette figure est déjà intégrée au document."),
    ).toBeVisible();
  });

  it("shows the closed stale refusal, offers a refresh, and never falls back silently", async () => {
    const user = userEvent.setup();
    const captured = installFetch(() =>
      response({ detail: { code: "enrichment_target_stale" } }, 409),
    );
    const onRefresh = vi.fn();
    renderDialog({ onRefresh });

    const dialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    await user.click(
      await within(dialog).findByRole("radio", {
        name: "Choisir Carte de remplacement",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Remplacer l’image" }),
    );

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(
      "Cette figure a changé depuis l’ouverture de la galerie. Actualisez l’atelier avant de réessayer.",
    );
    expect(alert).not.toHaveTextContent("enrichment_target_stale");
    expect(captured.acceptBodies).toEqual([]);
    expect(
      within(dialog).getByRole("button", { name: "Remplacer l’image" }),
    ).toBeEnabled();
    await user.click(
      within(dialog).getByRole("button", { name: "Actualiser l’atelier" }),
    );
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("announces a revoked figure instead of silently choosing another", async () => {
    const user = userEvent.setup();
    installFetch(() =>
      response({ detail: { code: "editorial_figure_not_found" } }, 404),
    );
    renderDialog();

    const dialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    await user.click(
      await within(dialog).findByRole("radio", {
        name: "Choisir Carte de remplacement",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Remplacer l’image" }),
    );
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("retirée ou révoquée");
    expect(
      within(dialog).getByRole("radio", {
        name: "Choisir Carte de remplacement",
      }),
    ).toBeChecked();
  });

  it("announces an accept conflict without closing the comparison", async () => {
    const user = userEvent.setup();
    installFetch(
      () => response(proposalRead()),
      () => response({ detail: { code: "proposal_state_conflict" } }, 409),
    );
    renderDialog();

    const dialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    await user.click(
      await within(dialog).findByRole("radio", {
        name: "Choisir Carte de remplacement",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Remplacer l’image" }),
    );
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(
      "La proposition a changé. Actualisez-la avant de décider.",
    );
    expect(
      within(dialog).getByRole("region", { name: "Comparaison des images" }),
    ).toBeVisible();
  });

  it("shows the shared BRIEF counter and never invents a cap for ARTICLE", async () => {
    installFetch();
    const first = renderDialog({
      snapshot: { ...snapshot, publication_kind: "brief" },
    });

    const dialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    expect(
      await within(dialog).findByText(
        /Capacité du document : 1\/3 figures intégrées · 2 places restantes\. Le remplacement conserve ce total\./,
      ),
    ).toBeVisible();
    first.unmount();

    renderDialog();
    const articleDialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    expect(
      within(articleDialog).queryByText(/Capacité du document/),
    ).toBeNull();
  });

  it("confirms before closing with a pending replacement selection", async () => {
    const user = userEvent.setup();
    installFetch();
    const onClose = vi.fn();
    renderDialog({ onClose });

    const dialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    await user.click(
      await within(dialog).findByRole("radio", {
        name: "Choisir Carte de remplacement",
      }),
    );

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
    expect(
      within(dialog).getByText(/Fermer le remplacement sans l’appliquer/),
    ).toBeVisible();
    const keep = within(dialog).getByRole("button", {
      name: "Continuer le remplacement",
    });
    await waitFor(() => expect(keep).toHaveFocus());

    await user.click(
      within(dialog).getByRole("button", {
        name: "Fermer sans enregistrer",
      }),
    );
    expect(onClose).toHaveBeenCalledOnce();
  });
});

describe("ReplaceFigureDialog keyboard", () => {
  it("selects and confirms with the keyboard only", async () => {
    const user = userEvent.setup();
    installFetch();
    renderDialog();

    const dialog = await screen.findByRole("dialog", {
      name: "Remplacer l’image intégrée",
    });
    const radio = await within(dialog).findByRole("radio", {
      name: "Choisir Carte de remplacement",
    });
    radio.focus();
    await user.keyboard(" ");
    expect(radio).toBeChecked();
    within(dialog).getByRole("button", { name: "Remplacer l’image" }).focus();
    await user.keyboard("{Enter}");
    expect(
      await within(dialog).findByText(
        "Image remplacée. Le document contient toujours exactement une image à cet emplacement.",
      ),
    ).toBeVisible();
  });
});
