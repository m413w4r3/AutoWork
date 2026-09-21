import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  SelectionBoard as SelectionBoardData,
  SelectionItem,
} from "../../api/selection";
import { SelectionBoard } from "./SelectionBoard";

const EDITION_ID = "edition-1";

function item(
  id: string,
  state: SelectionItem["state"],
  overrides: Partial<SelectionItem> = {},
): SelectionItem {
  return {
    discovery_subject_id: id,
    title: `Sujet ${id}`,
    summary: "Présentation du sujet.",
    presentation: null,
    actor_or_campaign: "Campagne Orion",
    publications: [
      {
        title: "Publication source",
        url: `https://example.test/${id}`,
        publisher: "Source",
        role: "primary",
        published_at: "2026-09-01T00:00:00Z",
      },
    ],
    candidate_count: 2,
    technical_potential: 3,
    technical_potential_reason: "Chaîne exploitable",
    announced_artifacts: ["loader"],
    publisher_ioc_count_total: 4,
    publisher_ioc_counts: [4],
    provisional_ioc_count: 1,
    provisional_ioc_type_counts: { domain: 1 },
    provisional_iocs: [
      {
        raw_value: "example.test",
        normalized_value: "example.test",
        proposed_type: "domain",
        declared_type: null,
        warnings: [],
      },
    ],
    uncertainties: ["Date à confirmer"],
    recommendation: "Traiter après validation.",
    state,
    subject_id: state === "selected" ? `subject-${id}` : null,
    last_decision: state === "undecided" ? null : lastDecision(id, state),
    updated_since_decision: false,
    selectable: state === "undecided",
    blocking_reason: null,
    ...overrides,
  };
}

function lastDecision(
  id: string,
  state: SelectionItem["state"],
): SelectionItem["last_decision"] {
  return {
    id: `decision-${id}`,
    decision: state === "selected" ? "select" : "ignore",
  };
}

const board: SelectionBoardData = {
  edition_id: EDITION_ID,
  snapshot_id: "snapshot-7",
  snapshot_version: 7,
  counts: { undecided: 2, ignored: 1, selected: 1, total: 4 },
  recommendation: "Traiter les sujets sélectionnables.",
  items: [
    item("undecided", "undecided", { updated_since_decision: true }),
    item("ignored", "ignored"),
    item("selected", "selected"),
    item("blocked", "undecided", {
      selectable: false,
      blocking_reason: "Fusion doit être résolue.",
    }),
  ],
};

function renderBoard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <SelectionBoard editionId={EDITION_ID} />
    </QueryClientProvider>,
  );
  return client;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SelectionBoard", () => {
  it("présente les états, recommandations, changements et liens canoniques", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(Response.json(board))),
    );

    renderBoard();

    expect(
      await screen.findByRole("heading", { name: "Sujet undecided" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("À décider").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Ignoré").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Sujet créé").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("link", { name: "Ouvrir Fusion" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Traiter les sujets sélectionnables."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Mis à jour depuis la décision"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sujet créé" })).toHaveAttribute(
      "href",
      "/subjects/subject-selected",
    );
    expect(screen.getByRole("link", { name: "Ouvrir Fusion" })).toHaveAttribute(
      "href",
      `/editions/${EDITION_ID}/fusion`,
    );
    expect(screen.getByRole("button", { name: "Traiter" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ignorer" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Traiter finalement" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "À résoudre dans Fusion" }),
    ).not.toBeInTheDocument();

    const forbidden = [
      ["Fusion", "ner"].join(""),
      ["Sé", "parer"].join(""),
      ["Lancer", " la production"].join(""),
      ["Article", " principal"].join(""),
      ["Br", "ève"].join(""),
    ];
    for (const phrase of forbidden) {
      expect(screen.queryByText(new RegExp(phrase))).not.toBeInTheDocument();
    }
  });

  it("confirme toutes les décisions en un seul lot avec snapshot, attentes et clé", async () => {
    const updated = {
      ...board,
      counts: { ...board.counts, undecided: 1, selected: 2 },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(Response.json(board))
      .mockResolvedValueOnce(Response.json(updated))
      .mockResolvedValue(Response.json(updated));
    vi.stubGlobal("fetch", fetchMock);

    renderBoard();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Traiter" }));
    await user.click(
      screen.getByRole("button", { name: "Traiter finalement" }),
    );
    await user.click(
      screen.getByRole("button", { name: /Confirmer les décisions \(2\)/ }),
    );

    await waitFor(() =>
      expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2),
    );
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe(`/api/editions/${EDITION_ID}/selection/decisions`);
    expect(init.method).toBe("POST");
    const headers = init.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(typeof headers["Idempotency-Key"]).toBe("string");
    expect(headers["Idempotency-Key"]).not.toBe("");
    expect(init.body).toBe(
      JSON.stringify({
        snapshot_version: 7,
        decisions: [
          {
            discovery_subject_id: "undecided",
            decision: "select",
            expected_decision_id: null,
          },
          {
            discovery_subject_id: "ignored",
            decision: "select",
            expected_decision_id: "decision-ignored",
          },
        ],
      }),
    );
    expect(init.body).not.toContain("Idempotency-Key");
  });

  it("GET la board en mode archivé et garde seulement la lecture", async () => {
    const fetchMock = vi.fn(() => Promise.resolve(Response.json(board)));
    vi.stubGlobal("fetch", fetchMock);

    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <SelectionBoard editionId={EDITION_ID} readOnly />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText(
        "Consultation historique : les décisions ne sont pas modifiables.",
      ),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/editions/${EDITION_ID}/selection`,
      undefined,
    );
    expect(
      screen.queryByRole("button", { name: "Traiter" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Ignorer" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Sujet créé" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Ouvrir Fusion" }),
    ).toBeInTheDocument();
  });

  it.each(["selection_snapshot_stale", "selection_decision_stale"] as const)(
    "recharge les décisions après %s sans conserver le brouillon",
    async (code) => {
      const staleResponse = new Response(
        JSON.stringify({ detail: { code, message: "Réponse obsolète" } }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      );
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(Response.json(board))
        .mockResolvedValueOnce(staleResponse)
        .mockResolvedValue(Response.json(board));
      vi.stubGlobal("fetch", fetchMock);

      renderBoard();
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "Traiter" }));
      await user.click(
        screen.getByRole("button", { name: /Confirmer les décisions/ }),
      );

      expect(await screen.findByText(/Rechargez la page/)).toBeInTheDocument();
      await waitFor(() =>
        expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(3),
      );
      expect(
        screen.getByRole("button", { name: "Confirmer les décisions (0)" }),
      ).toBeDisabled();
    },
  );
});
