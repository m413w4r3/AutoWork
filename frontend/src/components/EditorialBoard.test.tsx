import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
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

afterEach(() => vi.unstubAllGlobals());

it("avertit sur la couverture et permet une brève et un article principal", async () => {
  const board = {
    groups,
    selected_briefs: 0,
    selected_major: 0,
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
      "Les métadonnées et comptes IOC de découverte sont provisoires. Ils seront vérifiés depuis les documents archivés après la sélection.",
    ),
  ).toBeInTheDocument();
  expect(screen.getByText("Campagne du mois précédent")).toBeInTheDocument();

  const first = screen
    .getByRole("heading", { name: "Campagne A" })
    .closest("article")!;
  await user.click(
    within(first).getByRole("button", { name: "Sélectionner comme brève" }),
  );
  const second = screen
    .getByRole("heading", { name: "Campagne B" })
    .closest("article")!;
  await user.selectOptions(
    within(second).getByLabelText("Format éditorial"),
    "major",
  );
  await user.click(
    within(second).getByRole("button", {
      name: "Sélectionner comme article principal",
    }),
  );

  expect(postedBodies).toEqual([
    { editorial_type: "brief" },
    { editorial_type: "major" },
  ]);
});
