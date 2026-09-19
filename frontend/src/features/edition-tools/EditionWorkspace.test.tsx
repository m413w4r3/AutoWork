import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EditorialBoardResult, EditorialGroup } from "../../api/editorial";
import type { Edition } from "../../api/editions";
import {
  EditionNavigation,
  EditionToolSurface,
  EditionWorkspace,
  type EditionTool,
} from "./EditionWorkspace";

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

vi.mock("../../components/EditorialBoard", () => ({
  EditorialBoard: () => <div data-testid="editorial-board" />,
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

function editorialGroup(
  id: string,
  title: string,
  subjectId: string | null,
  status: EditorialGroup["status"],
): EditorialGroup {
  return {
    id,
    edition_id: EDITION_ID,
    title,
    outcome: "new_subject",
    status,
    subject_id: subjectId,
    candidates: [],
    score: {
      impact: 0,
      novelty: 0,
      technical_depth: 0,
      hunting_potential: 0,
      actionability: 0,
      source_quality: 0,
      total: 0,
      justifications: {},
    },
    source_relationship_status: "verified",
    needs_source_verification: false,
    needs_source_expansion: false,
    grouping_confidence: "high",
    grouping_justification: "Test",
    historical_comparison: null,
    version: 1,
  };
}

const board: EditorialBoardResult = {
  groups: [
    editorialGroup("group-a", "Sujet A", "subject-a", "selected"),
    editorialGroup("group-b", "Sujet B", "subject-b", "selected"),
    editorialGroup("group-c", "Sujet C", "subject-c", "rejected"),
  ],
  selected_articles: 2,
  ignored: 1,
  undecided: 0,
  automatic_selection: false,
};

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

  it("rend la sélection indépendamment des autres capacités", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(Response.json(board))),
    );

    renderSurface("selection");

    expect(
      await screen.findByRole("heading", { name: "2 sujets éligibles" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("editorial-board")).toBeInTheDocument();
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

  it("transmet uniquement les sujets cochés dans l’ordre éditorial puis ouvre la supervision", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (init?.method === "POST") {
        return Promise.resolve(Response.json({ batch_id: "batch-1" }));
      }
      if (url.includes("editorial-groups")) {
        return Promise.resolve(Response.json(board));
      }
      return Promise.resolve(Response.json({}));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderSurface("selection");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("checkbox", { name: "Sujet B" }));
    await user.click(screen.getByRole("checkbox", { name: "Sujet A" }));
    await user.click(
      screen.getByRole("button", { name: "Lancer la production de 2 sujets" }),
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
      ).toBe(true),
    );
    const post = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(post?.[1]?.body).toBe(
      JSON.stringify({ subject_ids: ["subject-a", "subject-b"] }),
    );
    await waitFor(() =>
      expect(window.location.pathname).toBe(
        `/editions/${EDITION_ID}/production`,
      ),
    );
  });

  it("ne montre pas les contrôles de lot pour une édition archivée", async () => {
    const archived: Edition = { ...edition, state: "archived" };

    renderSurface("selection", archived);

    await waitFor(() =>
      expect(screen.getByTestId("editorial-board")).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("heading", { name: /sujet.*éligible/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /production/i }),
    ).not.toBeInTheDocument();
  });
});
