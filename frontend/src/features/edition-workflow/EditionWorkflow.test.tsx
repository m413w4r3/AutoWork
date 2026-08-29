import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
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
  target_major_articles: 2,
  target_briefs: 3,
  previous_edition_id: null,
  source_profile: "default",
  status: "selection",
  version: 3,
  progress_percent: 30,
  allowed_transitions: ["production", "archived"],
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
      editorial_type: "brief" as const,
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
  selected_briefs: 1,
  selected_major: 0,
  ignored: 0,
  undecided: 0,
  target_briefs: 3,
  target_major: 2,
  automatic_selection: false as const,
};

const batch = {
  batch_id: "batch-1",
  edition_id: edition.id,
  profile: "brief_auto",
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
      retry_stage: null,
      error_code: null,
      error_message: null,
    },
  ],
  can_accept: true,
};

const release = {
  edition_id: edition.id,
  edition_status: "assembling" as const,
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

function editionWith(
  status: Edition["status"],
  allowed_transitions: Edition["allowed_transitions"] = [],
): Edition {
  return { ...edition, status, allowed_transitions };
}

function renderWorkflow(value: Edition) {
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
});

it("lance tous les sujets éligibles et invalide Edition après le POST", async () => {
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

  render(
    <QueryClientProvider client={client}>
      <EditionWorkflow edition={edition} />
    </QueryClientProvider>,
  );

  const start = await screen.findByRole("button", {
    name: "Lancer la production de 1 article",
  });
  expect(
    screen.queryByRole("button", { name: /traiter/i }),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  await user.click(start);

  await vi.waitFor(() => {
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["edition", edition.id],
    });
  });
  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
  expect(post?.[1]?.body).toBe(JSON.stringify({ subject_ids: null }));
});

describe("rendu strict des états Edition", () => {
  it("DRAFT affiche seulement l’introduction et la transition vers DISCOVERY", async () => {
    const updated = editionWith("discovery", ["selection"]);
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input);
      if (url.includes("/discovery/candidates")) {
        return Promise.resolve(Response.json(emptyDiscovery));
      }
      if (init?.method === "POST")
        return Promise.resolve(Response.json(updated));
      throw new Error(`Unexpected GET ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <EditionWorkflow edition={editionWith("draft", ["discovery"])} />
      </QueryClientProvider>,
    );

    expect(screen.getByText("Préparer la découverte")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Démarrer la découverte" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Sujets candidats")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        urlOf(input).includes("/discovery/candidates"),
      ),
    ).toBe(false);

    await user.click(
      screen.getByRole("button", { name: "Démarrer la découverte" }),
    );
    await waitFor(() =>
      expect(client.getQueryData(["edition", edition.id])).toEqual(updated),
    );
  });

  it("DISCOVERY désactive la sélection pendant un job puis la réactive à sa fin", async () => {
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
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.includes("/discovery/candidates"))
        return Promise.resolve(Response.json(emptyDiscovery));
      if (url.includes(`/jobs/${activeJobId}`))
        return Promise.resolve(Response.json(job));
      throw new Error(`Unexpected GET ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWorkflow(editionWith("discovery", ["selection"]));

    const openSelection = await screen.findByRole("button", {
      name: "Ouvrir la sélection",
    });
    expect(openSelection).toBeDisabled();
    expect(
      screen.getByText(
        "La recherche en cours doit se terminer avant la sélection.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => expect(openSelection).toBeEnabled());
  });

  it("SELECTION affiche le board et un seul lancement de production", async () => {
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
    renderWorkflow(editionWith("selection", ["production"]));
    expect(
      await screen.findByRole("button", {
        name: "Lancer la production de 1 article",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /sélectionner|traiter/i }),
    ).not.toBeInTheDocument();
  });

  it("PRODUCTION affiche uniquement la console métier", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(batch)));
    renderWorkflow(editionWith("production"));
    expect(
      await screen.findByRole("heading", { name: "0 / 1 articles traités" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Sujets candidats")).not.toBeInTheDocument();
    expect(screen.queryByText("Campagne A")).not.toBeInTheDocument();
  });

  it("REVIEW affiche la console de revue sans découverte ni sélection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(review)));
    renderWorkflow(editionWith("review"));
    expect(
      await screen.findByRole("heading", { name: "Revue de publication" }),
    ).toBeInTheDocument();
    expect(screen.getByText("1 inclus")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Accepter la production" }),
    ).toBeEnabled();
    expect(screen.queryByText("Sujets candidats")).not.toBeInTheDocument();
    expect(screen.queryByText("Campagne A")).not.toBeInTheDocument();
  });

  it("ASSEMBLING charge le release et affiche l’état d’assemblage", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(release)));
    renderWorkflow(editionWith("assembling"));
    expect(
      await screen.findByRole("heading", { name: "Manifest figé" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Assemblage du bulletin")).toBeInTheDocument();
    expect(screen.getByText("En attente")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("PUBLISHED affiche son état sans action si le DOCX n’est pas disponible", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...release,
          edition_status: "published",
          assembly_status: "succeeded",
        }),
      ),
    );
    renderWorkflow(editionWith("published"));
    expect(
      await screen.findByRole("heading", { name: "Bulletin publié" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("ARCHIVED affiche son état en lecture seule", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(release)));
    renderWorkflow(editionWith("archived"));
    expect(
      await screen.findByRole("heading", { name: "Édition archivée" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
