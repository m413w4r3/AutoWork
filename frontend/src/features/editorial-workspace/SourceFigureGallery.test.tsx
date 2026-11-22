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
  EditorialFigureCatalogPage,
  EditorialFigurePreview,
} from "../../api/editorialWorkspace";
import { SourceFigureGallery } from "./SourceFigureGallery";

const SUBJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const AVAILABLE_ID = "11111111-1111-4111-8111-111111111111";
const SECOND_AVAILABLE_ID = "99999999-9999-4999-8999-999999999999";
const INELIGIBLE_ID = "22222222-2222-4222-8222-222222222222";
const USED_ID = "55555555-5555-4555-8555-555555555555";

const catalog: EditorialFigureCatalogPage = {
  items: [
    {
      figure_id: AVAILABLE_ID,
      original_name: "Carte réseau originale",
      original_caption: "Flux observé dans le rapport.",
      alt_text: "Carte de réseau synthétique",
      source: {
        source_id: "33333333-3333-4333-8333-333333333333",
        title: "Rapport publié",
        url: "https://source.example/report",
      },
      page: 4,
      section: "Infrastructure",
      context_before: "Le texte précédant la carte.",
      context_after: "Le texte qui suit la carte.",
      width: 800,
      height: 450,
      mime_type: "image/png",
      status: "available",
      selectable: true,
      exclusion_reason: null,
      already_used: false,
    },
    {
      figure_id: SECOND_AVAILABLE_ID,
      original_name: "Schéma du réseau",
      original_caption: "Topologie observée.",
      alt_text: "Schéma de réseau synthétique",
      source: {
        source_id: "33333333-3333-4333-8333-333333333333",
        title: "Rapport publié",
        url: "https://source.example/report",
      },
      page: 5,
      section: "Infrastructure",
      context_before: null,
      context_after: null,
      width: 640,
      height: 360,
      mime_type: "image/png",
      status: "available",
      selectable: true,
      exclusion_reason: null,
      already_used: false,
    },
    {
      figure_id: INELIGIBLE_ID,
      original_name: "Image trop volumineuse",
      original_caption: null,
      alt_text: null,
      source: {
        source_id: "44444444-4444-4444-8444-444444444444",
        title: null,
        url: null,
      },
      page: null,
      section: null,
      context_before: null,
      context_after: null,
      width: 10000,
      height: 10000,
      mime_type: "image/png",
      status: "ineligible",
      selectable: false,
      exclusion_reason: "Le fichier dépasse la taille maximale autorisée.",
      already_used: false,
    },
    {
      figure_id: USED_ID,
      original_name: "Carte déjà intégrée",
      original_caption: null,
      alt_text: null,
      source: {
        source_id: "33333333-3333-4333-8333-333333333333",
        title: "Rapport publié",
        url: "https://source.example/report",
      },
      page: 2,
      section: null,
      context_before: null,
      context_after: null,
      width: 600,
      height: 400,
      mime_type: "image/png",
      status: "already_integrated",
      selectable: false,
      exclusion_reason: null,
      already_used: true,
    },
  ],
  sources: [
    {
      source_id: "33333333-3333-4333-8333-333333333333",
      title: "Rapport publié",
      url: "https://source.example/report",
    },
  ],
  total: 4,
  available_count: 2,
  offset: 0,
  limit: 30,
};

const preview: EditorialFigurePreview = {
  ...catalog.items[0]!,
  preview_image_url: `/api/subjects/${SUBJECT_ID}/editor/figures/${AVAILABLE_ID}/preview/image`,
};

function renderGallery(
  onClose = vi.fn(),
  props: Partial<Parameters<typeof SourceFigureGallery>[0]> = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <button type="button">Images sources</button>
      <SourceFigureGallery
        subjectId={SUBJECT_ID}
        onClose={onClose}
        {...props}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SourceFigureGallery", () => {
  it("shows eligibility reasons and keeps selection local while previewing", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url.includes("/preview")) {
        return Promise.resolve(
          new Response(JSON.stringify(preview), {
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify(catalog), {
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    renderGallery();

    const dialog = await screen.findByRole("dialog", {
      name: "Galerie d’images sources",
    });
    const available = await screen.findByRole("checkbox", {
      name: "Sélectionner Carte réseau originale",
    });
    const ineligible = screen.getByRole("checkbox", {
      name: "Sélectionner Image trop volumineuse",
    });
    expect(ineligible).toBeDisabled();
    const exclusionReason = within(dialog).getByText(
      "Le fichier dépasse la taille maximale autorisée.",
    );
    expect(exclusionReason).toBeVisible();
    expect(ineligible).toHaveAttribute("aria-describedby", exclusionReason.id);
    const integratedStatus = within(dialog).getByText("Déjà intégrée", {
      selector: ".editorial-figure-status",
    });
    expect(integratedStatus).toBeVisible();
    expect(integratedStatus).not.toHaveTextContent("Déjà utilisée");

    await user.click(available);
    expect(within(dialog).getByText("1 image sélectionnée")).toBeVisible();
    await user.click(
      within(dialog).getAllByRole("button", { name: "Aperçu détaillé" })[0]!,
    );
    expect(
      await screen.findByText("Le texte précédant la carte."),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Aperçu détaillé" }),
    ).toHaveFocus();
    expect(within(dialog).getByText("1 image sélectionnée")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining(`/editor/figures/${AVAILABLE_ID}/preview`),
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("asks the workspace to close the gallery on Escape", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify(catalog), {
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    const onClose = vi.fn();
    renderGallery(onClose);
    const dialog = await screen.findByRole("dialog", {
      name: "Galerie d’images sources",
    });
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
  });

  it("marks excluded figures with a readable reason and never a source UUID", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify(catalog), {
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    renderGallery();

    const dialog = await screen.findByRole("dialog", {
      name: "Galerie d’images sources",
    });
    expect(
      await within(dialog).findByText(
        "Cette figure est déjà intégrée au document.",
      ),
    ).toBeVisible();
    expect(
      within(dialog).getAllByText("Source archivée").length,
    ).toBeGreaterThan(0);
    expect(
      within(dialog).queryByText(/44444444-4444-4444-8444-444444444444/),
    ).toBeNull();
  });

  it("uses a single radio selection in replacement mode", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify(catalog), {
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    const onSelectionChange = vi.fn();
    renderGallery(vi.fn(), {
      mode: "single",
      variant: "embedded",
      heading: "Choisir une image de remplacement",
      selectedFigures: [],
      onSelectionChange,
    });

    const first = await screen.findByRole("radio", {
      name: "Choisir Carte réseau originale",
    });
    const second = screen.getByRole("radio", {
      name: "Choisir Schéma du réseau",
    });
    await user.click(first);
    await user.click(second);
    expect(onSelectionChange).toHaveBeenLastCalledWith([
      expect.objectContaining({ figure_id: SECOND_AVAILABLE_ID }),
    ]);
    expect(
      screen.queryByRole("checkbox", {
        name: "Sélectionner Carte réseau originale",
      }),
    ).toBeNull();
  });

  it("blocks a selection beyond the remaining capacity and explains why", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify(catalog), {
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    renderGallery(vi.fn(), { selectionLimit: 1 });

    const dialog = await screen.findByRole("dialog", {
      name: "Galerie d’images sources",
    });
    const first = await within(dialog).findByRole("checkbox", {
      name: "Sélectionner Carte réseau originale",
    });
    expect(
      within(dialog).getByText(
        "Sélection : 0/1 · cette insertion peut encore ajouter 1 figure.",
      ),
    ).toBeVisible();
    await user.click(first);
    expect(
      within(dialog).getByRole("checkbox", {
        name: "Sélectionner Schéma du réseau",
      }),
    ).toBeDisabled();
    expect(
      within(dialog).getByText(
        /Sélection : 1\/1 — le plafond de sélection est atteint/,
      ),
    ).toBeVisible();
    expect(within(dialog).getByText("1 image sélectionnée")).toBeVisible();
  });

  it("confirms before discarding a dialog-owned selection", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify(catalog), {
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    const onClose = vi.fn();
    renderGallery(onClose);

    const dialog = await screen.findByRole("dialog", {
      name: "Galerie d’images sources",
    });
    await user.click(
      await within(dialog).findByRole("checkbox", {
        name: "Sélectionner Carte réseau originale",
      }),
    );
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
    expect(
      within(dialog).getByText(/Fermer la galerie sans intégrer la sélection/),
    ).toBeVisible();
    const keep = within(dialog).getByRole("button", {
      name: "Continuer la sélection",
    });
    await waitFor(() => expect(keep).toHaveFocus());

    await user.click(keep);
    expect(
      within(dialog).queryByText(
        /Fermer la galerie sans intégrer la sélection/,
      ),
    ).toBeNull();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await user.click(
      within(dialog).getByRole("button", {
        name: "Fermer sans enregistrer",
      }),
    );
    expect(onClose).toHaveBeenCalledOnce();
  });
});
