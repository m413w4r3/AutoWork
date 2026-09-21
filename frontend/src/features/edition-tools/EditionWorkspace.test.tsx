import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Edition } from "../../api/editions";
import {
  getEditionProduction,
  startProductionBatch,
  type ProductionBoard,
} from "../../api/production";
import {
  EditionNavigation,
  EditionToolSurface,
  EditionWorkspace,
  type EditionTool,
} from "./EditionWorkspace";

vi.mock("../../api/production", () => ({
  getEditionProduction: vi.fn(),
  startProductionBatch: vi.fn(),
}));

vi.mock("../discovery/DiscoveryPanel", () => ({
  DiscoveryPanel: ({
    editionId,
    readOnly,
  }: {
    editionId: string;
    readOnly?: boolean;
  }) => (
    <div data-testid="tool-discovery" data-read-only={String(readOnly)}>
      Discovery {editionId}
    </div>
  ),
}));

vi.mock("../fusion/FusionBoard", () => ({
  FusionBoard: ({
    editionId,
    readOnly,
  }: {
    editionId: string;
    readOnly?: boolean;
  }) => (
    <div data-testid="tool-fusion" data-read-only={String(readOnly)}>
      Fusion {editionId}
    </div>
  ),
}));

vi.mock("../selection/SelectionBoard", () => ({
  SelectionBoard: ({
    editionId,
    readOnly,
  }: {
    editionId: string;
    readOnly?: boolean;
  }) => (
    <div data-testid="tool-selection" data-read-only={String(readOnly)}>
      Selection {editionId}
    </div>
  ),
}));

vi.mock("../edition-workflow/ProductionConsole", () => ({
  ProductionConsole: ({
    editionId,
    readOnly,
  }: {
    editionId: string;
    readOnly?: boolean;
  }) => (
    <div data-testid="tool-production" data-read-only={String(readOnly)}>
      Production {editionId}
    </div>
  ),
}));

vi.mock("../edition-workflow/ReviewConsole", () => ({
  ReviewConsole: ({
    editionId,
    readOnly,
  }: {
    editionId: string;
    readOnly?: boolean;
  }) => (
    <div data-testid="tool-review" data-read-only={String(readOnly)}>
      Review {editionId}
    </div>
  ),
}));

vi.mock("../edition-workflow/PublicationConsole", () => ({
  PublicationConsole: ({
    editionId,
    readOnly,
  }: {
    editionId: string;
    readOnly?: boolean;
  }) => (
    <div data-testid="tool-publication" data-read-only={String(readOnly)}>
      Publication {editionId}
    </div>
  ),
}));

const EDITION_ID = "edition-1";

const edition: Edition = {
  id: EDITION_ID,
  country: "France",
  country_code: "FR",
  period_start: "2026-08-01",
  period_end: "2026-08-31",
  tlp: "GREEN",
  languages: ["fr"],
  state: "open",
  version: 3,
  created_at: "2026-08-29T10:00:00Z",
  updated_at: "2026-08-29T10:00:00Z",
};

const productionBoard: ProductionBoard = {
  edition_id: EDITION_ID,
  subjects: [
    {
      title: "Sujet A",
      subject_id: "subject-a",
      tlp: "GREEN",
      latest_run_id: null,
      latest_run_number: null,
      latest_status: null,
      latest_stage: null,
      active_run_id: null,
      can_start: true,
      blocking_reason: null,
    },
    {
      title: "Sujet B",
      subject_id: "subject-b",
      tlp: "GREEN",
      latest_run_id: null,
      latest_run_number: null,
      latest_status: null,
      latest_stage: null,
      active_run_id: null,
      can_start: true,
      blocking_reason: null,
    },
    {
      title: "Sujet ignoré",
      subject_id: "subject-ignored",
      tlp: "GREEN",
      latest_run_id: null,
      latest_run_number: null,
      latest_status: null,
      latest_stage: null,
      active_run_id: null,
      can_start: false,
      blocking_reason: "Sujet déjà en production",
    },
  ],
  active_batch: null,
  recent_batches: [],
};

beforeEach(() => {
  vi.mocked(getEditionProduction).mockResolvedValue(productionBoard);
  vi.mocked(startProductionBatch).mockReturnValue(new Promise<never>(() => {}));
});

function renderSurface(tool: EditionTool, value = edition) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <EditionToolSurface edition={value} tool={tool} />
    </QueryClientProvider>,
  );
  return client;
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
  window.localStorage.clear();
});

describe("EditionNavigation", () => {
  it("expose les hrefs des capacités et marque seulement la destination courante", () => {
    render(<EditionNavigation editionId={EDITION_ID} current="selection" />);

    expect(
      screen.getByRole("link", { name: "Vue d’ensemble" }),
    ).toHaveAttribute("href", `/editions/${EDITION_ID}`);
    expect(screen.getByRole("link", { name: "Découverte" })).toHaveAttribute(
      "href",
      `/editions/${EDITION_ID}/discovery`,
    );
    expect(screen.getByRole("link", { name: "Fusion" })).toHaveAttribute(
      "href",
      `/editions/${EDITION_ID}/fusion`,
    );
    expect(screen.getByRole("link", { name: "Sélection" })).toHaveAttribute(
      "href",
      `/editions/${EDITION_ID}/selection`,
    );
    expect(screen.getByRole("link", { name: "Productions" })).toHaveAttribute(
      "href",
      `/editions/${EDITION_ID}/production`,
    );
    expect(screen.getByRole("link", { name: "Revue" })).toHaveAttribute(
      "href",
      `/editions/${EDITION_ID}/review`,
    );
    expect(screen.getByRole("link", { name: "Publication" })).toHaveAttribute(
      "href",
      `/editions/${EDITION_ID}/publication`,
    );
    expect(screen.getByRole("link", { name: "Sélection" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      screen
        .getAllByRole("link")
        .filter((link) => link.getAttribute("aria-current") !== null),
    ).toHaveLength(1);
  });

  it("reste une navigation non ordonnée et sans sémantique d’étape", () => {
    const { container } = render(
      <EditionNavigation editionId={EDITION_ID} current="overview" />,
    );

    expect(container.querySelector("ol")).toBeNull();
    expect(container.querySelector('[aria-current="step"]')).toBeNull();
    expect(
      screen.queryByText(/workflow|étape|suivant/i),
    ).not.toBeInTheDocument();
    expect(
      screen
        .getAllByRole("link")
        .every((link) => !link.hasAttribute("aria-disabled")),
    ).toBe(true);
  });
});

describe("EditionToolSurface", () => {
  it.each([
    ["discovery", "tool-discovery"],
    ["fusion", "tool-fusion"],
    ["selection", "tool-selection"],
    ["production", "tool-production"],
    ["review", "tool-review"],
    ["publication", "tool-publication"],
  ] as const)("rend directement l’outil %s", (tool, testId) => {
    renderSurface(tool);
    expect(screen.getByTestId(testId)).toHaveAttribute(
      "data-read-only",
      "false",
    );
  });

  it("rend Fusion en lecture seule pour une édition archivée", () => {
    renderSurface("fusion", { ...edition, state: "archived" });
    expect(screen.getByTestId("tool-fusion")).toHaveAttribute(
      "data-read-only",
      "true",
    );
  });

  it("rend la sélection indépendamment des autres capacités", () => {
    renderSurface("selection");
    expect(screen.getByTestId("tool-selection")).toHaveAttribute(
      "data-read-only",
      "false",
    );
  });

  it("n’expose aucun contrôle de production sur la surface de sélection", () => {
    renderSurface("selection");

    expect(
      screen.queryByRole("button", { name: /Démarrer le lot de production/i }),
    ).not.toBeInTheDocument();
  });

  it("liste les sujets sélectionnés et soumet un sous-ensemble dans l’ordre canonique", async () => {
    renderSurface("production");

    await waitFor(() =>
      expect(
        screen.getByRole("checkbox", { name: "Sujet A" }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("checkbox", { name: "Sujet B" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Sujet ignoré")).not.toBeInTheDocument();
    expect(screen.getByText("Sujet déjà en production")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: "Sujet B" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Sujet A" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Démarrer le lot de production" }),
      ).not.toBeDisabled(),
    );
    screen
      .getByRole("button", { name: "Démarrer le lot de production" })
      .click();

    await waitFor(() => {
      expect(startProductionBatch).toHaveBeenCalledTimes(1);
      expect(startProductionBatch).toHaveBeenCalledWith(
        EDITION_ID,
        ["subject-a", "subject-b"],
        expect.any(String),
      );
    });
  });

  it("ne propose pas de démarrer une production pour une édition archivée", async () => {
    renderSurface("production", { ...edition, state: "archived" });

    await waitFor(() =>
      expect(screen.getByTestId("tool-production")).toHaveAttribute(
        "data-read-only",
        "true",
      ),
    );
    expect(
      screen.queryByRole("button", { name: /Démarrer le lot de production/i }),
    ).not.toBeInTheDocument();
  });

  it("laisse la sélection disponible pendant une découverte active", () => {
    // Aucun état local ne porte plus l'identité d'une découverte : l'historique
    // des runs vient de l'API, et les outils restent indépendants.
    render(
      <QueryClientProvider client={new QueryClient()}>
        <EditionWorkspace edition={edition} current="discovery" />
      </QueryClientProvider>,
    );

    expect(screen.getByTestId("tool-discovery")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sélection" })).toHaveAttribute(
      "href",
      `/editions/${EDITION_ID}/selection`,
    );
    expect(
      screen.queryByText(
        "La recherche en cours doit se terminer avant la sélection.",
      ),
    ).not.toBeInTheDocument();
  });

  it("rend la sélection en lecture seule pour une édition archivée", () => {
    const archived: Edition = { ...edition, state: "archived" };

    renderSurface("selection", archived);
    expect(screen.getByTestId("tool-selection")).toHaveAttribute(
      "data-read-only",
      "true",
    );
  });
});
