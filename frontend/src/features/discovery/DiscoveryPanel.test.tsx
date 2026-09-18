import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  DiscoveryCandidate,
  DiscoveryInputMode,
  DiscoveryRun,
} from "../../api/discovery";
import type { JobStatus } from "../../api/jobs";
import { DiscoveryPanel } from "./DiscoveryPanel";

const editionId = "edition-discovery-test";
const jobId = "job-discovery-test";

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.href
      : input.url;
}

function snapshot(axis: string): DiscoveryRun["request_snapshot"] {
  return {
    country: "Iran",
    country_code: "IR",
    country_aliases: ["Iran"],
    period_start: "2026-07-01",
    period_end: "2026-07-31",
    as_of_date: "2026-09-18",
    languages: ["fr"],
    source_profile: "default-profile",
    keywords: [],
    exclusions: [],
    complementary_axis: axis,
    tlp: "AMBER",
    sensitivity: "internal",
    external_llm_allowed: true,
  };
}

function run(
  id: string,
  status: JobStatus,
  options: {
    axis: string;
    inputMode?: DiscoveryInputMode;
    result?: DiscoveryRun["result"];
    userMessage?: string | null;
    errorCode?: string | null;
    errorMessage?: string | null;
  },
): DiscoveryRun {
  const inputMode: DiscoveryInputMode = options.inputMode ?? "bridge_research";
  return {
    run_id: id,
    edition_id: editionId,
    input_mode: inputMode,
    source_profile: "default-profile",
    complementary_axis: options.axis,
    created_by: "dev-analyst",
    created_at:
      id === "newest" ? "2026-09-18T10:00:00Z" : "2026-09-17T10:00:00Z",
    request_snapshot: snapshot(options.axis),
    execution:
      inputMode === "manual_import"
        ? null
        : {
            job_id: jobId,
            status,
            progress_current: status === "running" ? 2 : 4,
            progress_total: 4,
            user_message: options.userMessage ?? null,
            error_code: options.errorCode ?? null,
            error_message: options.errorMessage ?? null,
            error_details: null,
            started_at: null,
            finished_at: null,
          },
    result: options.result ?? null,
  };
}

function renderPanel(readOnly = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DiscoveryPanel editionId={editionId} readOnly={readOnly} />
    </QueryClientProvider>,
  );
}

function candidate(
  id: string,
  runId: string,
  title: string,
  contextOnly = false,
): DiscoveryCandidate {
  return {
    id,
    discovery_run_id: runId,
    discovery_batch_id: `batch-${runId}`,
    created_at: "2026-09-18T10:00:00Z",
    title,
    summary: "Résumé brut.",
    novelty: "Nouvelle information.",
    technical_potential: 3,
    event_date: "2026-09-12",
    uncertainties: [],
    relevance_reasons: [],
    actors: ["Acteur"],
    campaigns: [],
    malware: [],
    cves: [],
    victims: [],
    sectors: [],
    countries: [],
    likely_artifacts: ["IOC"],
    iocs: [],
    sources: [],
    incomplete_sources: [],
    local_ref: "S1",
    actor_or_campaign: "Acteur",
    technical_potential_reason: "Potentiel.",
    parsing_warnings: [],
    context_only: contextOnly,
    selectable: !contextOnly,
    valid_publication_count: 0,
    incomplete_publication_count: 0,
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("DiscoveryPanel durable run history", () => {
  it("renders newest-first history and every execution projection", async () => {
    const runs = [
      run("newest", "running", {
        axis: "new-axis",
        userMessage: "Recherche en cours",
      }),
      run("waiting", "waiting_human", { axis: "human-axis" }),
      run("failed", "failed", {
        axis: "failed-axis",
        errorCode: "bridge_timeout",
        errorMessage: "Bridge indisponible",
      }),
      run("succeeded", "succeeded", {
        axis: "succeeded-axis",
        result: {
          batch_id: "batch-succeeded",
          research_model_run_id: "model-run-succeeded",
          archived_report_url: "/reports/succeeded.md",
        },
      }),
      run("manual", "succeeded", {
        axis: "manual-axis",
        inputMode: "manual_import",
        result: {
          batch_id: "batch-manual",
          research_model_run_id: "model-run-manual",
          archived_report_url: "/reports/manual.md",
        },
      }),
    ];
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.endsWith("/discovery/runs"))
        return Promise.resolve(Response.json(runs));
      if (url.includes(`/api/jobs/${jobId}`)) {
        return Promise.resolve(
          Response.json({
            id: jobId,
            kind: "discover_edition",
            aggregate_type: "edition",
            aggregate_id: editionId,
            status: "running",
            progress_current: 2,
            progress_total: 4,
            user_message: "Recherche en cours",
            attempt: 1,
            max_attempts: 1,
            next_retry_at: null,
            started_at: null,
            finished_at: null,
            heartbeat_at: null,
            error_code: null,
            error_message: null,
            error_details: null,
            correlation_id: "correlation",
            output_reference: null,
            cancellation_requested: false,
            created_at: "2026-09-18T10:00:00Z",
            updated_at: "2026-09-18T10:00:00Z",
          }),
        );
      }
      if (/\/discovery\/runs\/[^/]+\/candidates/.test(url))
        return Promise.resolve(Response.json([]));
      if (url.includes("/discovery/candidates"))
        return Promise.resolve(
          Response.json({
            batches: [],
            candidates: [],
            total: 0,
            warning: "",
          }),
        );
      if (url.includes("/editorial-groups"))
        return Promise.resolve(
          Response.json({
            groups: [],
            selected_articles: 0,
            ignored: 0,
            undecided: 0,
            automatic_selection: false,
          }),
        );
      return Promise.resolve(Response.json([]));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPanel();

    await screen.findByText("new-axis");
    const history = await screen.findByRole("heading", {
      name: "Historique des recherches",
    });
    expect(history).toBeInTheDocument();
    const historyItems = history.parentElement?.querySelector("ol")?.children;
    expect(historyItems?.[0]).toHaveTextContent("new-axis");
    expect(historyItems?.[1]).toHaveTextContent("human-axis");
    expect(screen.getByText(/Progression : 2\/4/)).toBeInTheDocument();
    expect(screen.getAllByText(/Recherche en cours/).length).toBeGreaterThan(0);
    expect(
      screen.getByText(/L’intervention d’un analyste est requise/),
    ).toBeInTheDocument();
    expect(screen.getByText(/bridge_timeout/)).toBeInTheDocument();
    expect(
      screen.getAllByRole("link", {
        name: "Consulter le rapport Markdown archivé",
      })[0],
    ).toHaveAttribute("href", "/reports/succeeded.md");
    expect(
      screen.getByText(/Import manuel · résultat disponible/),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("link", {
        name: "Consulter le rapport Markdown archivé",
      }),
    ).toHaveLength(2);
  });

  it("shows a newly launched run without localStorage and refetches after remount", async () => {
    let runs: DiscoveryRun[] = [];
    let runListCalls = 0;
    let candidateCalls = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      if (url.endsWith("/discovery/runs") && init?.method === "POST") {
        const created = run("newest", "queued", { axis: "initial" });
        runs = [created];
        return Promise.resolve(Response.json(created, { status: 202 }));
      }
      if (url.endsWith("/discovery/runs")) {
        runListCalls += 1;
        return Promise.resolve(Response.json(runs));
      }
      if (/\/discovery\/runs\/[^/]+\/candidates/.test(url)) {
        candidateCalls += 1;
        return Promise.resolve(
          Response.json([
            candidate("remount-candidate", "newest", "Candidat persistant"),
          ]),
        );
      }
      if (url.includes("/discovery/candidates"))
        return Promise.resolve(
          Response.json({ batches: [], candidates: [], total: 0, warning: "" }),
        );
      if (url.includes("/editorial-groups"))
        return Promise.resolve(Response.json({ groups: [] }));
      return Promise.resolve(Response.json([]));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const view = renderPanel();

    await user.click(
      await screen.findByRole("button", { name: "Nouvelle recherche ChatGPT" }),
    );
    expect(await screen.findByText("initial")).toBeInTheDocument();
    expect(await screen.findByText("Candidat persistant")).toBeInTheDocument();
    expect(window.localStorage.length).toBe(0);
    const callsBeforeRemount = runListCalls;
    const candidateCallsBeforeRemount = candidateCalls;
    view.unmount();
    renderPanel();
    await waitFor(() =>
      expect(runListCalls).toBeGreaterThan(callsBeforeRemount),
    );
    await waitFor(() =>
      expect(candidateCalls).toBeGreaterThan(candidateCallsBeforeRemount),
    );
    expect(await screen.findByText("Candidat persistant")).toBeInTheDocument();
  });

  it("keeps raw candidates distinct, shows their run origins and filters through the API", async () => {
    const runs = [
      run("newest", "succeeded", { axis: "axe-nouveau" }),
      run("older", "succeeded", { axis: "axe-ancien" }),
    ];
    const rawCandidates = [
      candidate("candidate-new", "newest", "Même campagne — vague A"),
      candidate("candidate-old", "older", "Même campagne — vague B", true),
    ];
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = requestUrl(input);
      if (url.endsWith("/discovery/runs"))
        return Promise.resolve(Response.json(runs));
      if (url.includes("/discovery/runs/newest/candidates"))
        return Promise.resolve(Response.json([rawCandidates[0]]));
      if (url.includes("/discovery/runs/older/candidates"))
        return Promise.resolve(Response.json([rawCandidates[1]]));
      if (url.includes("/discovery/candidates")) {
        const filtered = new URL(url, "http://localhost").searchParams.has(
          "search",
        )
          ? [rawCandidates[0]]
          : rawCandidates;
        return Promise.resolve(
          Response.json({
            batches: [],
            candidates: filtered,
            total: filtered.length,
            warning: "",
          }),
        );
      }
      if (url.includes("/editorial-groups"))
        return Promise.resolve(Response.json({ groups: [] }));
      return Promise.resolve(Response.json([]));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPanel();

    // Also listed under the selected run's history, hence findAll.
    expect(
      (await screen.findAllByText("Même campagne — vague A")).length,
    ).toBeGreaterThan(0);
    await user.click(screen.getByText("Détails techniques de la découverte"));
    expect(screen.getByText("Même campagne — vague B")).toBeInTheDocument();
    expect(screen.getByText(/newest · axe-nouveau/)).toBeInTheDocument();
    expect(screen.getByText(/older · axe-ancien/)).toBeInTheDocument();
    expect(screen.getByText("Contexte uniquement")).toBeInTheDocument();
    expect(screen.queryByText("Découverte cumulée")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Recherche"), "même");
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) =>
          requestUrl(input).includes("search=m%C3%AAme"),
        ),
      ).toBe(true),
    );
    expect(
      screen.queryByText("Même campagne — vague B"),
    ).not.toBeInTheDocument();
  });

  it("renders archived history in read-only mode and disables launch", async () => {
    const historical = run("succeeded", "succeeded", {
      axis: "archived-axis",
      result: {
        batch_id: "batch-archived",
        research_model_run_id: "model-run-archived",
        archived_report_url: "/reports/archived.md",
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = requestUrl(input);
        if (url.endsWith("/discovery/runs"))
          return Promise.resolve(Response.json([historical]));
        if (/\/discovery\/runs\/[^/]+\/candidates/.test(url))
          return Promise.resolve(Response.json([]));
        if (url.includes("/discovery/candidates"))
          return Promise.resolve(
            Response.json({
              batches: [],
              candidates: [],
              total: 0,
              warning: "",
            }),
          );
        if (url.includes("/editorial-groups"))
          return Promise.resolve(Response.json({ groups: [] }));
        return Promise.resolve(Response.json([]));
      }),
    );

    renderPanel(true);

    expect(await screen.findByText("archived-axis")).toBeInTheDocument();
    expect(
      screen.getByRole("link", {
        name: "Consulter le rapport Markdown archivé",
      }),
    ).toHaveAttribute("href", "/reports/archived.md");
    expect(
      screen.queryByRole("button", { name: "Nouvelle recherche ChatGPT" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Coller une réponse ChatGPT" }),
    ).not.toBeInTheDocument();
  });
});
