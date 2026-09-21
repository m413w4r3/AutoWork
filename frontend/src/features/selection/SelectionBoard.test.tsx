import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SelectionBoard } from "./SelectionBoard";

const EDITION_ID = "edition-1";

/*
 * Fixtures are the payload `backend/src/cti_app/api/selection.py` really
 * serializes — `effective_state`, `member_candidate_ids`, an object
 * recommendation and top-level counters. Anything the UI needs is derived
 * from it by the normalizer, never by the test.
 */

type WireItem = Record<string, unknown>;

function wireItem(
  id: string,
  effectiveState: "undecided" | "ignored" | "selected",
  overrides: WireItem = {},
): WireItem {
  return {
    discovery_subject_id: id,
    canonical_discovery_subject_id: id,
    title: `Sujet ${id}`,
    summary: "Présentation du sujet.",
    actor_or_campaign: "Campagne Orion",
    technical_potential: 3,
    technical_potential_reason: "Chaîne exploitable",
    artifacts: ["loader"],
    publications: [
      {
        url: `https://example.test/${id}`,
        title: "Publication source",
        publisher: "Source",
        role: "primary",
        tlp: "AMBER",
        sensitivity: "internal",
        external_llm_allowed: true,
        published_at: "2026-09-01",
        event_date: null,
        citation: null,
        local_ref: null,
        source_ref: `source-${id}`,
        raw_url: `https://example.test/${id}`,
        period_relation: "unknown",
        ioc_presence: "declared",
        ioc_declared_count: 4,
        ioc_visible_count: null,
        parsing_warnings: [],
        markdown_block: null,
        id: `publication-${id}`,
        verification_status: "unverified",
        relationship_status: "provisional",
        verification_changed_at: null,
        verification_changed_by: null,
        canonical_url: `https://example.test/${id}`,
        title_fingerprint: null,
      },
    ],
    provisional_iocs: [
      {
        raw_value: "example.test",
        normalized_value: "example.test",
        declared_type: "domain",
        proposed_type: "domain",
        publication_relations: [],
        model_run_id: null,
        markdown_block: "block",
        warnings: [],
        status: "provisional_visible",
        id: `ioc-${id}`,
      },
    ],
    uncertainties: ["Date à confirmer"],
    selectable: effectiveState === "undecided",
    blocking_reason: null,
    effective_state: effectiveState,
    subject_id: effectiveState === "selected" ? `subject-${id}` : null,
    recommendation:
      effectiveState === "undecided"
        ? { recommended: true, reason: "ioc_signal" }
        : { recommended: false, reason: null },
    last_decision:
      effectiveState === "undecided"
        ? null
        : wireLastDecision(id, effectiveState),
    updated_since_decision: false,
    member_candidate_ids: [`candidate-${id}-1`, `candidate-${id}-2`],
    ...overrides,
  };
}

function wireLastDecision(id: string, state: "ignored" | "selected"): WireItem {
  return {
    id: `decision-${id}`,
    action: state === "selected" ? "select" : "ignore",
    snapshot_id: "snapshot-7",
    snapshot_version: 7,
    subject_id: state === "selected" ? `subject-${id}` : null,
    actor_id: "analyst-1",
    occurred_at: "2026-09-02T10:00:00Z",
  };
}

const board = {
  edition_id: EDITION_ID,
  snapshot_id: "snapshot-7",
  snapshot_version: 7,
  fusion_review_count: 1,
  selected: 1,
  ignored: 1,
  undecided: 2,
  items: [
    wireItem("undecided", "undecided", { updated_since_decision: true }),
    wireItem("ignored", "ignored"),
    wireItem("selected", "selected"),
    wireItem("blocked", "undecided", {
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
      screen.getByText("1 fusion(s) à résoudre avant de décider."),
    ).toBeInTheDocument();
    // Derived from the real wire payload, not sent by the API.
    expect(screen.getAllByText("2").length).toBeGreaterThan(0);
    expect(
      screen.getAllByText("4 annoncés · 1 provisoire").length,
    ).toBeGreaterThan(0);
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
    const updated = { ...board, undecided: 1, selected: 2 };
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
            action: "select",
            expected_decision_id: null,
          },
          {
            discovery_subject_id: "ignored",
            action: "select",
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
