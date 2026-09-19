import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  FusionBoard as FusionBoardModel,
  FusionCandidate,
} from "../../api/fusion";
import { FusionBoard } from "./FusionBoard";

const editionId = "edition-1";
const APPLY = "Appliquer les décisions";
const MERGE = "Fusionner les groupes sélectionnés";
const SPLIT = "Séparer les candidates sélectionnées";
const STALE =
  "L’état de fusion a changé. Reprenez la décision sur l’état rafraîchi.";

function candidate(id: string, title: string): FusionCandidate {
  return {
    id,
    discovery_run_id: "run-1",
    discovery_batch_id: "batch-1",
    supersedes_candidate_id: null,
    title,
    summary: `${title} résumé`,
    event_date: "2026-09-04",
    actors: ["APT-X"],
    campaigns: [],
    malware: ["Foo"],
    cves: ["CVE-2026-1234"],
    iocs: ["203.0.113.10"],
    countries: [],
    sectors: [],
    likely_artifacts: [],
    publications: [
      {
        id: `${id}-publication`,
        url: `https://example.test/${id}`,
        canonical_url: `https://example.test/${id}`,
        title: `${title} publication`,
        publisher: "Vendor A",
        published_at: "2026-09-04",
      },
    ],
  };
}

const board: FusionBoardModel = {
  edition_id: editionId,
  snapshot_id: "snapshot-7",
  snapshot_version: 7,
  read_only: false,
  candidate_count: 4,
  group_count: 2,
  pending_review_count: 1,
  groups: [
    {
      discovery_subject_id: "subject-a",
      title: "Campagne A",
      summary: "Deux publications sur la même campagne.",
      candidate_ids: ["candidate-a", "candidate-c"],
      candidates: [
        candidate("candidate-a", "Candidate A"),
        candidate("candidate-c", "Candidate C"),
      ],
      confidence: "high",
      origin: "heuristic",
      resolution_state: "established",
      deterministic_signals: [
        {
          kind: "shared_actor",
          value: "APT-X",
          candidate_ids: ["candidate-a", "candidate-c"],
        },
      ],
      model_suggestion: null,
      differences: ["Malware « Bar » absent de « Candidate A »"],
      history: [
        {
          action: "created",
          merge_run_id: "run-created",
          planner_kind: "heuristic",
          actor_id: null,
          candidate_ids: [],
          created_at: "2026-09-01T10:00:00+00:00",
        },
      ],
    },
    {
      discovery_subject_id: "subject-b",
      title: "Campagne B",
      summary: "Une publication isolée.",
      candidate_ids: ["candidate-b"],
      candidates: [candidate("candidate-b", "Candidate B")],
      confidence: null,
      origin: null,
      resolution_state: "established",
      deterministic_signals: [],
      model_suggestion: null,
      differences: [],
      history: [],
    },
  ],
  pending_reviews: [
    {
      merge_run_id: "review-1",
      candidate_ids: ["candidate-d"],
      discovery_subject_ids: ["subject-a"],
      review_reasons: ["confidence_medium"],
      stale: false,
      created_at: "2026-09-02T10:00:00+00:00",
      groups: [
        {
          candidate_ids: ["candidate-d"],
          candidates: [candidate("candidate-d", "Candidate D")],
          proposed_discovery_subject_ids: ["subject-a"],
          confidence: "medium",
          requires_decision: true,
          deterministic_signals: [
            {
              kind: "shared_cve",
              value: "CVE-2026-1234",
              candidate_ids: ["candidate-d", "candidate-a"],
            },
          ],
          model_suggestion: {
            recommendation: "attach",
            summary: "Même campagne probable.",
          },
          differences: ["Les dates diffèrent de 2 jour(s)"],
        },
      ],
    },
  ],
  unstabilized_candidates: [],
};

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  return input instanceof URL ? input.href : input.url;
}

function staleResponse(): Response {
  const detail = { code: "fusion_snapshot_stale", message: "Périmé" };
  return Response.json({ detail }, { status: 409 });
}

function stubFetch(onPost?: () => Response, data = board) {
  const posts: Array<{ url: string; body: unknown }> = [];
  let gets = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        const body: unknown = JSON.parse(init.body as string);
        posts.push({ url: urlOf(input), body });
        return Promise.resolve(onPost ? onPost() : Response.json(data));
      }
      gets += 1;
      return Promise.resolve(Response.json(data));
    }),
  );
  return { posts, gets: () => gets };
}

function renderBoard(readOnly = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <FusionBoard editionId={editionId} readOnly={readOnly} />
    </QueryClientProvider>,
  );
}

async function decide(label: string) {
  const user = userEvent.setup();
  const radio = await screen.findByRole("radio", { name: label });
  await waitFor(() => expect(radio).toBeEnabled());
  await user.click(radio);
  await user.click(screen.getByRole("button", { name: APPLY }));
}

afterEach(() => vi.unstubAllGlobals());

describe("FusionBoard", () => {
  it("explique les groupes sans décision éditoriale ni handle technique", async () => {
    stubFetch();
    renderBoard();

    expect(
      await screen.findByRole("heading", { name: "Fusion explicable" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Campagne A" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("table")).toHaveLength(2);
    expect(screen.getAllByText("Signaux déterministes")).toHaveLength(3);
    expect(screen.getAllByText("Suggestion du modèle")).toHaveLength(3);
    expect(screen.getByText("Acteur commun : APT-X (2)")).toBeInTheDocument();
    expect(
      screen.getByText("Recommandation : rattacher. Même campagne probable."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Malware « Bar » absent de « Candidate A »"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Rattachement proposé à « Campagne A »"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Article|Ignorer|Lancer la production/),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/\b[CX]1\b/)).not.toBeInTheDocument();
  });

  it("résout un cas à revoir avec des UUID métier et le snapshot affiché", async () => {
    const fetched = stubFetch();
    renderBoard();
    const user = userEvent.setup();

    const apply = await screen.findByRole("button", { name: APPLY });
    expect(apply).toBeDisabled();
    await user.click(
      screen.getByRole("radio", { name: "Rattacher à un groupe existant" }),
    );
    expect(apply).toBeDisabled();
    await user.selectOptions(
      screen.getByLabelText("Groupe cible"),
      "subject-a",
    );
    expect(
      screen.getByText("Effet : les candidates rejoignent le groupe choisi."),
    ).toBeInTheDocument();
    await user.click(apply);
    await waitFor(() => expect(fetched.posts).toHaveLength(1));
    expect(fetched.posts[0]?.url).toBe(
      `/api/editions/${editionId}/fusion/reviews/review-1/resolve`,
    );
    expect(fetched.posts[0]?.body).toEqual({
      snapshot_version: 7,
      decisions: [
        {
          action: "attach",
          candidate_ids: ["candidate-d"],
          target_discovery_subject_id: "subject-a",
        },
      ],
    });

    await decide("Séparer");
    await waitFor(() => expect(fetched.posts).toHaveLength(2));
    await decide("Différer");
    await waitFor(() => expect(fetched.posts).toHaveLength(3));
    await decide("Accepter le regroupement");
    await waitFor(() => expect(fetched.posts).toHaveLength(4));
    expect(fetched.posts[1]?.body).toEqual({
      snapshot_version: 7,
      decisions: [{ action: "separate", candidate_ids: ["candidate-d"] }],
    });
    expect(fetched.posts[2]?.body).toMatchObject({
      decisions: [{ action: "defer" }],
    });
    expect(fetched.posts[3]?.body).toMatchObject({
      decisions: [{ action: "accept" }],
    });
  });

  it("fusionne des groupes et sépare des candidates par UUID", async () => {
    const fetched = stubFetch();
    renderBoard();
    const user = userEvent.setup();

    const groupChecks = await screen.findAllByRole("checkbox", {
      name: "Sélectionner pour fusion",
    });
    for (const check of groupChecks) await user.click(check);
    await user.click(screen.getByRole("button", { name: MERGE }));
    await waitFor(() => expect(fetched.posts).toHaveLength(1));
    expect(fetched.posts[0]?.url).toBe(
      `/api/editions/${editionId}/fusion/merge`,
    );
    expect(fetched.posts[0]?.body).toEqual({
      snapshot_version: 7,
      discovery_subject_ids: ["subject-a", "subject-b"],
    });

    const splitCheck = screen.getByRole("checkbox", { name: "Candidate C" });
    await waitFor(() => expect(splitCheck).toBeEnabled());
    await user.click(splitCheck);
    await user.click(screen.getByRole("button", { name: SPLIT }));
    await waitFor(() => expect(fetched.posts).toHaveLength(2));
    expect(fetched.posts[1]?.url).toBe(
      `/api/editions/${editionId}/fusion/split`,
    );
    expect(fetched.posts[1]?.body).toEqual({
      snapshot_version: 7,
      discovery_subject_id: "subject-a",
      candidate_ids: ["candidate-c"],
    });
  });

  it("rafraîchit le board et efface les choix après un snapshot périmé", async () => {
    const fetched = stubFetch(staleResponse);
    renderBoard();
    const user = userEvent.setup();

    const groupChecks = await screen.findAllByRole("checkbox", {
      name: "Sélectionner pour fusion",
    });
    for (const check of groupChecks) await user.click(check);
    await user.click(screen.getByRole("button", { name: MERGE }));

    expect(await screen.findByText(STALE)).toBeInTheDocument();
    await waitFor(() => expect(fetched.gets()).toBe(2));
    for (const check of groupChecks) expect(check).not.toBeChecked();
  });

  it("masque toutes les mutations en lecture seule", async () => {
    stubFetch();
    renderBoard(true);
    await expectReadOnly();
  });

  it("masque toutes les mutations d’une édition archivée", async () => {
    stubFetch(undefined, { ...board, read_only: true });
    renderBoard();
    await expectReadOnly();
  });
});

async function expectReadOnly() {
  expect(
    await screen.findByText("Archive · lecture seule"),
  ).toBeInTheDocument();
  expect(screen.getByText("Acteur commun : APT-X (2)")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /Appliquer|Fusionner|Séparer/ }),
  ).not.toBeInTheDocument();
  expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  expect(screen.queryAllByRole("radio")).toHaveLength(0);
}
