import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Edition } from "../../api/editions";
import type { JobView } from "../../api/jobs";
import { discoveryJobStorageKey } from "../discovery/discoveryStorage";
import { EditionWorkflow } from "./EditionWorkflow";

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

const edition: Edition = {
  id: "edition-1",
  country: "Iran",
  country_code: "IR",
  period_start: "2026-08-01",
  period_end: "2026-08-31",
  tlp: "AMBER",
  languages: ["fr"],
  state: "open",
  version: 3,
  created_at: "2026-08-29T10:00:00Z",
  updated_at: "2026-08-29T10:00:00Z",
};

const board = {
  groups: [
    {
      id: "group-1",
      edition_id: edition.id,
      title: "Campagne A",
      outcome: "new_subject" as const,
      status: "selected" as const,
      subject_id: "subject-1",
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
      source_relationship_status: "verified" as const,
      needs_source_verification: false,
      needs_source_expansion: false,
      grouping_confidence: "high" as const,
      grouping_justification: "",
      historical_comparison: null,
      version: 1,
    },
  ],
  selected_articles: 1,
  ignored: 0,
  undecided: 0,
  automatic_selection: false as const,
};

const batch = {
  batch_id: "batch-1",
  edition_id: edition.id,
  status: "queued" as const,
  phase: "initial" as const,
  next_dispatch_at: null,
  items: 1,
  completed: 0,
  needs_review: 0,
  failed: 0,
  cancelled: 0,
  item_details: [],
  created_at: "2026-08-29T10:00:00Z",
  started_at: null,
  finished_at: null,
};

const review = {
  edition_id: edition.id,
  items: [
    {
      position: 1,
      subject_id: "subject-1",
      title: "Sujet prêt",
      run_id: "run-1",
      pipeline_generation: 1,
      run_status: "ready" as const,
      document_artifact_id: "artifact-1",
      document_artifact_version: 1,
      document_input_hash: "a".repeat(64),
      effective_decision_id: null,
      effective_decision: "include" as const,
      included: true,
      blocking: false,
      can_retry: false,
      requires_reconciliation: false,
      reconciliation: null,
      retry_stage: null,
      error_code: null,
      error_message: null,
    },
  ],
  can_accept: true,
};

const release = {
  edition_id: edition.id,
  edition_state: "open" as const,
  manifest_id: "manifest-1",
  manifest_sha256: "a".repeat(64),
  release_id: null,
  json_available: false,
  markdown_available: false,
  docx_available: false,
  published_at: null,
  assembly_job_id: "job-assembly",
  assembly_status: "queued" as const,
  assembly_error_code: null,
  assembly_error_message: null,
  can_retry_assembly: false,
};

const emptyDiscovery = {
  batches: [],
  candidates: [],
  total: 0,
  merge_stats: {
    raw_batch_count: 0,
    raw_candidate_count: 0,
    consolidated_candidate_count: 0,
    unique_publication_count: 0,
    duplicate_publication_occurrence_count: 0,
  },
  warning: "",
};

function editionWith(state: Edition["state"]): Edition {
  return { ...edition, state };
}

function renderWorkflow(value: Edition, phase?: string) {
  if (phase) {
    window.history.replaceState({}, "", `/editions/${value.id}?phase=${phase}`);
  }
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <EditionWorkflow edition={value} />
    </QueryClientProvider>,
  );
  return client;
}

afterEach(() => {
  window.localStorage.clear();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/editions/edition-1");
});

it("exige de cocher le sujet éligible avant de lancer, puis invalide Edition après le POST", async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = urlOf(input);
    if (url.includes("/editorial-groups")) return Response.json(board);
    if (init?.method === "POST") return Response.json(batch);
    throw new Error(`Unexpected GET ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const user = userEvent.setup();
  window.history.replaceState({}, "", "/editions/edition-1?phase=selection");

  render(
    <QueryClientProvider client={client}>
      <EditionWorkflow edition={edition} />
    </QueryClientProvider>,
  );

  const disabledStart = await screen.findByRole("button", {
    name: "Sélectionnez au moins un article",
  });
  expect(disabledStart).toBeDisabled();
  expect(screen.getByText("0 sélectionné pour ce lot")).toBeInTheDocument();

  const checkbox = await screen.findByRole("checkbox", {
    name: "Campagne A",
  });
  expect(checkbox).not.toBeChecked();
  await user.click(checkbox);

  const start = await screen.findByRole("button", {
    name: "Lancer la production de 1 article",
  });
  expect(screen.getByText("1 sélectionné pour ce lot")).toBeInTheDocument();
  await user.click(start);

  await vi.waitFor(() => {
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["edition", edition.id],
    });
  });
  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
  expect(post?.[1]?.body).toBe(JSON.stringify({ subject_ids: ["subject-1"] }));
});

describe("rendu strict des états Edition", () => {
  it("DRAFT affiche directement l’outil de découverte sans transition Edition", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (url.includes("/discovery/candidates")) {
        return Promise.resolve(Response.json(emptyDiscovery));
      }
      if (init?.method === "POST") {
        throw new Error(`Unexpected workflow mutation ${url}`);
      }
      throw new Error(`Unexpected GET ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWorkflow(editionWith("open"));

    expect(await screen.findByText("Sujets candidats")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Démarrer la découverte" }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, callInit]) => callInit?.method === "POST"),
    ).toBe(false);
  });

  it("DISCOVERY rend l’outil directement sans action de transition", async () => {
    const activeJobId = "job-discovery";
    window.localStorage.setItem(
      discoveryJobStorageKey(edition.id),
      activeJobId,
    );
    const job: JobView = {
      id: activeJobId,
      kind: "discover_edition",
      aggregate_type: "edition",
      aggregate_id: edition.id,
      status: "succeeded",
      progress_current: 1,
      progress_total: 1,
      user_message: null,
      attempt: 1,
      max_attempts: 1,
      next_retry_at: null,
      started_at: "2026-08-29T10:00:00Z",
      finished_at: "2026-08-29T10:01:00Z",
      heartbeat_at: null,
      error_code: null,
      error_message: null,
      error_details: null,
      correlation_id: "correlation-1",
      output_reference: null,
      cancellation_requested: false,
      created_at: "2026-08-29T10:00:00Z",
      updated_at: "2026-08-29T10:01:00Z",
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        throw new Error(`Unexpected workflow mutation ${urlOf(input)}`);
      }
      const url = urlOf(input);
      if (url.includes("/discovery/candidates"))
        return Promise.resolve(Response.json(emptyDiscovery));
      if (url.includes(`/jobs/${activeJobId}`))
        return Promise.resolve(Response.json(job));
      throw new Error(`Unexpected GET ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWorkflow(editionWith("open"));

    expect(await screen.findByText("Sujets candidats")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Ouvrir la sélection" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "La recherche en cours doit se terminer avant la sélection.",
      ),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, callInit]) => callInit?.method === "POST"),
    ).toBe(false);
  });

  it("SELECTION affiche le board et un seul sélecteur de lot de production, non pré-armé", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = urlOf(input);
        if (url.includes("/editorial-groups"))
          return Promise.resolve(Response.json(board));
        if (init?.method === "POST")
          return Promise.resolve(Response.json(batch));
        throw new Error(`Unexpected GET ${url}`);
      }),
    );
    renderWorkflow(editionWith("open"), "selection");
    expect(
      await screen.findByRole("heading", { name: "1 article éligible" }),
    ).toBeInTheDocument();
    expect(screen.getByText("0 sélectionné pour ce lot")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Sélectionnez au moins un article" }),
    ).toBeDisabled();
    expect(screen.getAllByRole("checkbox")).toHaveLength(1);
    expect(screen.getByRole("checkbox")).not.toBeChecked();
  });

  it("PRODUCTION affiche uniquement la console métier", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(batch)));
    renderWorkflow(editionWith("open"), "production");
    expect(
      await screen.findByRole("heading", { name: "0 / 1 articles traités" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Sujets candidats")).not.toBeInTheDocument();
    expect(screen.queryByText("Campagne A")).not.toBeInTheDocument();
  });

  it("REVIEW affiche la console de revue sans découverte ni sélection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(review)));
    renderWorkflow(editionWith("open"), "review");
    expect(
      await screen.findByRole("heading", { name: "Revue de publication" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Articles bloquants/ }),
    ).toHaveTextContent("0");
    const acceptButton = screen.getByRole("button", {
      name: "Accepter la production",
    });
    await waitFor(() => expect(acceptButton).toBeEnabled());
    expect(screen.queryByText("Sujets candidats")).not.toBeInTheDocument();
    expect(screen.queryByText("Campagne A")).not.toBeInTheDocument();
  });

  it("assemblage en attente : charge le release et affiche l’état d’assemblage", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(release)));
    renderWorkflow(editionWith("open"), "publication");
    expect(
      await screen.findByRole("heading", { name: "Manifest figé" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Assemblage du bulletin")).toBeInTheDocument();
    expect(screen.getByText("En attente")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("assemblage réussi : affiche le bulletin publié sans action si le DOCX est indisponible", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...release,
          edition_state: "open",
          release_id: "release-1",
          assembly_status: "succeeded",
        }),
      ),
    );
    renderWorkflow(editionWith("open"), "publication");
    expect(
      await screen.findByRole("heading", { name: "Bulletin publié" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("édition archivée : affiche son état en lecture seule", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(release)));
    renderWorkflow(editionWith("archived"), "publication");
    expect(
      await screen.findByRole("heading", { name: "Édition archivée" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("navigation historique du workflow", () => {
  function historyFetchMock() {
    return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (init?.method === "POST") {
        throw new Error(`Unexpected workflow mutation ${url}`);
      }
      if (url.endsWith("/discovery/candidates"))
        return Response.json(emptyDiscovery);
      if (url.endsWith("/release")) return Response.json(release);
      if (url.endsWith("/editorial-groups")) return Response.json(board);
      if (url.endsWith("/production")) return Response.json(batch);
      if (url.endsWith("/review")) return Response.json(review);
      throw new Error(`Unexpected GET ${url}`);
    });
  }

  it("permet de consulter les cinq phases d’une édition archivée sans mutation", async () => {
    const fetchMock = historyFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderWorkflow(editionWith("archived"));

    expect(await screen.findByText("Sujets candidats")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Découverte" })).toHaveAttribute(
      "aria-current",
      "step",
    );

    await user.click(screen.getByRole("link", { name: "Sélection" }));
    expect(
      await screen.findByRole("heading", { name: "Sélection des sujets" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Campagne A")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Ouvrir le sujet" }),
    ).toHaveAttribute("href", "/subjects/subject-1");
    expect(
      screen.queryByRole("button", { name: /Lancer la production/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Tout sélectionner" }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);

    await user.click(screen.getByRole("link", { name: "Production" }));
    expect(
      await screen.findByRole("heading", { name: "0 / 1 articles traités" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Arrêter|Réessayer|Récupérer/ }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Revue" }));
    expect(
      await screen.findByRole("heading", { name: "Revue de publication" }),
    ).toBeInTheDocument();
    expect(screen.getByText("État final de la revue")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: /Accepter|Réessayer|Exclure|Arrêter/,
      }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Publication" }));
    expect(
      await screen.findByRole("heading", { name: "Édition archivée" }),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);
  });

  it("ouvre la phase demandée par URL et retombe sur la découverte si elle est invalide", async () => {
    const fetchMock = historyFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/editions/edition-1?phase=production");

    renderWorkflow(editionWith("archived"));
    expect(
      await screen.findByRole("heading", { name: "0 / 1 articles traités" }),
    ).toBeInTheDocument();
    expect(window.location.search).toBe("?phase=production");
    cleanup();

    window.history.replaceState({}, "", "/editions/edition-1?phase=unknown");
    renderWorkflow(editionWith("archived"));
    expect(await screen.findByText("Sujets candidats")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Découverte" })).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);
  });

  it("laisse les outils actifs accessibles pour une édition ouverte", async () => {
    const fetchMock = historyFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderWorkflow(editionWith("open"), "production");
    expect(
      await screen.findByRole("heading", { name: "0 / 1 articles traités" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Arrêter et revenir à la sélection",
      }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Sélection" }));
    expect(
      await screen.findByRole("heading", { name: "Sélection des sujets" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Tout sélectionner" }),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);

    await user.click(screen.getByRole("link", { name: "Production" }));
    expect(
      await screen.findByRole("button", {
        name: "Arrêter et revenir à la sélection",
      }),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);

    window.history.back();
    expect(
      await screen.findByRole("heading", { name: "Sélection des sujets" }),
    ).toBeInTheDocument();
  });
});

describe("sélecteur du lot de production", () => {
  function groupOf(id: string, title: string, subjectId: string) {
    return {
      id,
      edition_id: edition.id,
      title,
      outcome: "new_subject" as const,
      status: "selected" as const,
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
      source_relationship_status: "verified" as const,
      needs_source_verification: false,
      needs_source_expansion: false,
      grouping_confidence: "high" as const,
      grouping_justification: "",
      historical_comparison: null,
      version: 1,
    };
  }

  function boardOf(groups: ReturnType<typeof groupOf>[]) {
    return {
      groups,
      selected_articles: groups.length,
      ignored: 0,
      undecided: 0,
      automatic_selection: false as const,
    };
  }

  const groupA = groupOf("group-a", "Article A", "subject-a");
  const groupB = groupOf("group-b", "Article B", "subject-b");
  const groupC = groupOf("group-c", "Article C", "subject-c");
  const boardABC = boardOf([groupA, groupB, groupC]);

  it("22 articles éligibles démarrent avec zéro présélectionné", async () => {
    const groups = Array.from({ length: 22 }, (_, index) =>
      groupOf(`group-${index}`, `Article ${index}`, `subject-${index}`),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json(boardOf(groups))),
    );
    renderWorkflow(editionWith("open"), "selection");

    expect(
      await screen.findByRole("heading", { name: "22 articles éligibles" }),
    ).toBeInTheDocument();
    expect(screen.getByText("0 sélectionné pour ce lot")).toBeInTheDocument();
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(22);
    for (const checkbox of checkboxes) expect(checkbox).not.toBeChecked();
    expect(
      screen.getByRole("button", { name: "Sélectionnez au moins un article" }),
    ).toBeDisabled();
  });

  it("cocher B puis A construit un payload dans l’ordre éditorial A, B", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (url.includes("/editorial-groups")) return Response.json(boardABC);
      if (init?.method === "POST") return Response.json(batch);
      throw new Error(`Unexpected GET ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderWorkflow(editionWith("open"), "selection");

    await screen.findByRole("heading", { name: "3 articles éligibles" });
    const checkboxB = screen.getByRole("checkbox", { name: "Article B" });
    expect(checkboxB.closest("label")).toHaveClass(
      "production-batch-selector__choice",
    );
    expect(checkboxB.closest("section")).toHaveClass(
      "production-batch-selector",
    );
    await user.click(checkboxB);
    await user.click(screen.getByRole("checkbox", { name: "Article A" }));

    expect(screen.getByText("2 sélectionnés pour ce lot")).toBeInTheDocument();
    const start = screen.getByRole("button", {
      name: "Lancer la production de 2 articles",
    });
    await user.click(start);

    await vi.waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([, init]) => init?.method === "POST",
      );
      expect(post?.[1]?.body).toBe(
        JSON.stringify({ subject_ids: ["subject-a", "subject-b"] }),
      );
    });

    // Editorial decisions of the untouched article C are never mutated: no
    // POST ever targets the editorial-groups decision/merge/split routes.
    const editorialMutations = fetchMock.mock.calls.filter(
      ([input, init]) =>
        init?.method === "POST" && urlOf(input).includes("/editorial-groups"),
    );
    expect(editorialMutations).toHaveLength(0);
  });

  it("désélectionner B ramène le compteur à 1", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(boardABC)));
    const user = userEvent.setup();
    renderWorkflow(editionWith("open"), "selection");

    await screen.findByRole("heading", { name: "3 articles éligibles" });
    await user.click(screen.getByRole("checkbox", { name: "Article A" }));
    await user.click(screen.getByRole("checkbox", { name: "Article B" }));
    expect(screen.getByText("2 sélectionnés pour ce lot")).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: "Article B" }));
    expect(screen.getByText("1 sélectionné pour ce lot")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Lancer la production de 1 article" }),
    ).toBeInTheDocument();
  });

  it("un article devenu non éligible est retiré automatiquement de la sélection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(boardABC)));
    const user = userEvent.setup();
    const client = renderWorkflow(editionWith("open"), "selection");

    await screen.findByRole("heading", { name: "3 articles éligibles" });
    await user.click(screen.getByRole("checkbox", { name: "Article A" }));
    await user.click(screen.getByRole("checkbox", { name: "Article B" }));
    expect(screen.getByText("2 sélectionnés pour ce lot")).toBeInTheDocument();

    // The board refreshes and B is no longer an editorially selected
    // article (still present, but proposed again) — the batch selection
    // must drop it without ever re-adding it silently.
    client.setQueryData(["editorial-board", edition.id], {
      ...boardABC,
      groups: [groupA, { ...groupB, status: "proposed" as const }, groupC],
    });

    await waitFor(() => {
      expect(screen.getByText("2 articles éligibles")).toBeInTheDocument();
      expect(screen.getByText("1 sélectionné pour ce lot")).toBeInTheDocument();
    });
    expect(
      screen.queryByRole("checkbox", { name: "Article B" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Lancer la production de 1 article" }),
    ).toBeInTheDocument();
  });
});
