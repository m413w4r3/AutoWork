import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { EditorialBoard } from "./EditorialBoard";

const groups = [
  {
    id: "11111111-1111-4111-8111-111111111111",
    edition_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    title: "Campagne A",
    outcome: "new_subject",
    status: "proposed",
    editorial_type: null,
    subject_id: null,
    presentation: "Présentation éditoriale A",
    actor_or_campaign: "MuddyWater",
    technical_potential: 4,
    technical_potential_reason: "IOC et configurations visibles.",
    artifacts: ["ioc", "configurations"],
    publications: [
      {
        title: "Publication A",
        url: "https://a.example/report",
        publisher: "Vendor Research",
        role: "primary",
        published_at: "2026-07-02",
      },
    ],
    uncertainties: ["Attribution à confirmer"],
    publisher_ioc_count_total: 52,
    provisional_ioc_count: 1,
    provisional_ioc_type_counts: { sha256: 1 },
    provisional_iocs: [
      {
        raw_value: "a".repeat(64),
        normalized_value: "a".repeat(64),
        proposed_type: "sha256",
        declared_type: "sha256",
        warnings: [],
      },
    ],
    metadata_incomplete: false,
    candidates: [
      {
        id: "cccccccc-cccc-4ccc-8ccc-ccccccccccc1",
        batch_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        title: "Publication A",
        summary: "Résumé A",
        event_date: "2026-07-02",
        source_urls: ["https://a.example/report"],
      },
    ],
    score: {
      impact: 3,
      novelty: 4,
      technical_depth: 4,
      hunting_potential: 3,
      actionability: 3,
      source_quality: 2,
      total: 19,
      justifications: {
        impact: "Secteur critique",
        novelty: "Nouvelle campagne",
        technical_depth: "Rapport technique",
        hunting_potential: "IOC visibles",
        actionability: "Mesures possibles",
        source_quality: "Relations provisoires",
      },
    },
    source_relationship_status: "provisional",
    needs_source_verification: true,
    needs_source_expansion: true,
    grouping_confidence: "medium",
    grouping_justification: "Titre et IOC proches",
    historical_comparison: {
      group_id: "99999999-9999-4999-8999-999999999999",
      title: "Campagne du mois précédent",
      editorial_type: "major",
      subject_id: "88888888-8888-4888-8888-888888888888",
    },
    version: 1,
  },
  {
    id: "22222222-2222-4222-8222-222222222222",
    edition_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    title: "Campagne B",
    outcome: "ambiguous_review",
    status: "proposed",
    editorial_type: null,
    subject_id: null,
    candidates: [
      {
        id: "cccccccc-cccc-4ccc-8ccc-ccccccccccc2",
        batch_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        title: "Publication B",
        summary: "Résumé B",
        event_date: "2026-07-03",
        source_urls: ["https://b.example/report"],
      },
    ],
    score: {
      impact: 2,
      novelty: 2,
      technical_depth: 2,
      hunting_potential: 2,
      actionability: 2,
      source_quality: 1,
      total: 11,
      justifications: {
        impact: "Impact limité",
        novelty: "À confirmer",
        technical_depth: "Quelques détails",
        hunting_potential: "Peu d’IOC",
        actionability: "À évaluer",
        source_quality: "Relations provisoires",
      },
    },
    source_relationship_status: "provisional",
    needs_source_verification: true,
    needs_source_expansion: true,
    grouping_confidence: "low",
    grouping_justification: "Cas ambigu conservé",
    historical_comparison: null,
    version: 1,
  },
] as const;

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("désactive le polling malgré la valeur globale de production", async () => {
  vi.useFakeTimers();
  const board = {
    groups: [],
    selected_briefs: 0,
    selected_major: 0,
    ignored: 0,
    undecided: 0,
    target_briefs: 4,
    target_major: 2,
    automatic_selection: false,
  };
  const fetchMock = vi.fn().mockResolvedValue(Response.json(board));
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: 1,
        refetchInterval: 30_000,
        refetchOnWindowFocus: false,
      },
    },
  });

  render(
    <QueryClientProvider client={client}>
      <EditorialBoard editionId="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" />
    </QueryClientProvider>,
  );

  await vi.waitFor(() => {
    expect(
      screen.getByText("Aucun groupe en attente de décision."),
    ).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  await act(() => vi.advanceTimersByTimeAsync(60_000));
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it("propose quatre choix exclusifs et confirme les décisions dans un seul lot", async () => {
  const board = {
    groups,
    selected_briefs: 0,
    selected_major: 0,
    ignored: 0,
    undecided: 2,
    target_briefs: 4,
    target_major: 2,
    automatic_selection: false,
  };
  const postedBodies: unknown[] = [];
  const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST" && typeof init.body === "string") {
      postedBodies.push(JSON.parse(init.body) as unknown);
    }
    return Promise.resolve(Response.json(board));
  });
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <EditorialBoard editionId="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" />
    </QueryClientProvider>,
  );

  expect(
    await screen.findByText(
      "IOC repérés pendant la recherche — non encore vérifiés depuis les sources.",
    ),
  ).toBeInTheDocument();
  expect(
    screen.getByText("52 annoncés · 1 valeur visible"),
  ).toBeInTheDocument();
  expect(screen.getByText("Campagne du mois précédent")).not.toBeVisible();

  const first = screen
    .getAllByRole("heading", { name: "Campagne A" })[0]!
    .closest("article")!;
  expect(within(first).getByRole("radio", { name: "À décider" })).toBeChecked();
  expect(
    within(first).getByRole("radio", { name: "Brève" }),
  ).toBeInTheDocument();
  expect(
    within(first).getByRole("radio", {
      name: "Article approfondi + pivots",
    }),
  ).toBeInTheDocument();
  expect(
    within(first).getByRole("radio", { name: "Ignorer" }),
  ).toBeInTheDocument();
  await user.click(within(first).getByRole("radio", { name: "Ignorer" }));
  expect(
    screen.getByRole("button", { name: "Confirmer la sélection (1)" }),
  ).toBeEnabled();
  await user.click(within(first).getByRole("radio", { name: "À décider" }));
  expect(
    screen.getByRole("button", { name: "Confirmer la sélection (0)" }),
  ).toBeDisabled();
  await user.click(within(first).getByRole("radio", { name: "Brève" }));
  const second = screen
    .getAllByRole("heading", { name: "Campagne B" })[0]!
    .closest("article")!;
  await user.click(
    within(second).getByRole("radio", {
      name: "Article approfondi + pivots",
    }),
  );
  await user.click(
    screen.getByRole("button", { name: "Confirmer la sélection (2)" }),
  );

  expect(postedBodies).toEqual([
    {
      decisions: [
        {
          group_id: "11111111-1111-4111-8111-111111111111",
          version: 1,
          decision: "brief",
        },
        {
          group_id: "22222222-2222-4222-8222-222222222222",
          version: 1,
          decision: "major",
        },
      ],
    },
  ]);
});
