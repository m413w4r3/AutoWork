import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { Edition } from "./api/editions";

const iranEdition: Edition = {
  id: "30e5b0b8-2dba-48c3-81ca-9eaed5c22c62",
  country: "Iran",
  country_code: "IR",
  period_start: "2026-07-01",
  period_end: "2026-07-31",
  tlp: "AMBER",
  languages: ["fr", "en", "fa"],
  target_major_articles: 2,
  target_briefs: 6,
  previous_edition_id: null,
  source_profile: "iran-default",
  status: "draft",
  version: 1,
  progress_percent: 0,
  allowed_transitions: ["discovery", "archived"],
  created_at: "2026-08-08T00:00:00Z",
  updated_at: "2026-08-08T00:00:00Z",
};

const emptyEditorialBoard = {
  groups: [],
  selected_briefs: 0,
  selected_major: 0,
  target_briefs: 6,
  target_major: 2,
  automatic_selection: false,
};

function renderApp() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.history.replaceState({}, "", "/editions");
});

describe("App éditions", () => {
  it("affiche la liste avec badges, progression et lien détail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          items: [iranEdition],
          total: 1,
          page: 1,
          page_size: 20,
        }),
      ),
    );
    renderApp();

    expect(
      await screen.findByRole("heading", { name: "Iran" }),
    ).toBeInTheDocument();
    expect(screen.getByText("TLP:AMBER")).toBeInTheDocument();
    expect(
      screen.getByText("Brouillon", { selector: "span" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveValue(0);
  });

  it("présente un état vide exploitable", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          Response.json({ items: [], total: 0, page: 1, page_size: 20 }),
        ),
    );
    renderApp();

    expect(
      await screen.findByRole("heading", { name: "Aucune édition" }),
    ).toBeInTheDocument();
  });

  it("crée une édition Iran et n’affiche que les transitions autorisées", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url === "/api/editions" && init?.method === "POST") {
        return Response.json(iranEdition, { status: 201 });
      }
      if (url.includes("/editorial-groups"))
        return Response.json(emptyEditorialBoard);
      if (url.endsWith(iranEdition.id)) return Response.json(iranEdition);
      return Response.json({ items: [], total: 0, page: 1, page_size: 20 });
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/editions/new");
    const user = userEvent.setup();
    renderApp();

    await user.type(screen.getByLabelText("Pays"), "Iran");
    await user.type(screen.getByLabelText("Code pays"), "IR");
    await user.type(screen.getByLabelText("Période"), "2026-07");
    await user.clear(screen.getByLabelText("Langues"));
    await user.type(screen.getByLabelText("Langues"), "fr,en,fa");
    await user.clear(screen.getByLabelText("Profil de sources"));
    await user.type(screen.getByLabelText("Profil de sources"), "iran-default");
    await user.click(screen.getByRole("button", { name: "Créer l’édition" }));

    expect(
      await screen.findByRole("heading", { name: "Iran" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Passer à « Découverte »" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Passer à « Archivée »" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Publiée/ }),
    ).not.toBeInTheDocument();
    const createCall = fetchMock.mock.calls.find(
      ([url, init]) => url === "/api/editions" && init?.method === "POST",
    );
    const requestBody = createCall?.[1]?.body;
    expect(
      JSON.parse(typeof requestBody === "string" ? requestBody : "{}"),
    ).toMatchObject({
      country: "Iran",
      country_code: "IR",
      period_start: "2026-07-01",
      period_end: "2026-07-31",
      languages: ["fr", "en", "fa"],
      previous_edition_id: null,
    });
  });

  it("rend les erreurs API accessibles", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          {
            detail: {
              code: "storage_error",
              message: "Service indisponible.",
            },
          },
          { status: 503 },
        ),
      ),
    );
    renderApp();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Service indisponible.",
    );
  });

  it("reprend le suivi d’une recherche après rechargement", async () => {
    const jobId = "20658589-a6d5-4af5-b026-d5c6fcb3b7f0";
    window.localStorage.setItem(`cti-discovery-job:${iranEdition.id}`, jobId);
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url.includes(`/api/jobs/${jobId}`))
        return Response.json({
          id: jobId,
          kind: "discover_edition",
          aggregate_type: "edition",
          aggregate_id: iranEdition.id,
          status: "succeeded",
          progress_current: 4,
          progress_total: 4,
          user_message: "Lot persisté",
          attempt: 1,
          max_attempts: 1,
          next_retry_at: null,
          started_at: "2026-08-10T10:00:00Z",
          finished_at: "2026-08-10T10:01:00Z",
          heartbeat_at: "2026-08-10T10:01:00Z",
          error_code: null,
          error_message: null,
          error_details: null,
          correlation_id: "reload-test",
          output_reference: "discovery-batch://batch",
          cancellation_requested: false,
          created_at: "2026-08-10T10:00:00Z",
          updated_at: "2026-08-10T10:01:00Z",
        });
      if (url.includes("/discovery/candidates"))
        return Response.json({
          batches: [],
          candidates: [],
          total: 0,
          warning: "",
        });
      if (url.includes("/editorial-groups"))
        return Response.json(emptyEditorialBoard);
      if (url.endsWith(iranEdition.id)) return Response.json(iranEdition);
      return Response.json({ items: [], total: 0, page: 1, page_size: 20 });
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/editions/${iranEdition.id}`);

    renderApp();

    expect(await screen.findByText("Terminée")).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Sélection des sujets" }),
    ).toBeInTheDocument();
    expect(
      window.localStorage.getItem(`cti-discovery-job:${iranEdition.id}`),
    ).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(`/api/jobs/${jobId}`);
  });

  it("lance la découverte et rend explicites sources et incertitudes non vérifiées", async () => {
    const candidateResult = {
      batches: [
        {
          id: "61cb719a-6432-4381-911e-d4447ecf6332",
          complementary_axis: "initial",
          queries: ["Iran APT July 2026 technical report"],
          citations: [
            {
              label: "Rapport original",
              url: "https://vendor.example/report",
              excerpt: "Rapport technique cité par le modèle.",
            },
          ],
          discovery_model_run_id: "f7fd2882-da41-4d3c-9bea-e592b6d2524a",
          structuring_model_run_id: "4c84c931-989b-498b-84b0-60901671321d",
          created_at: "2026-08-10T10:00:00Z",
          parsing_warnings: [],
          unattached_visible_citations: [
            {
              label: "Citation orpheline",
              url: "https://orphan.example/report",
              canonical_url: "https://orphan.example/report",
              excerpt: null,
            },
          ],
        },
      ],
      candidates: [
        {
          id: "c20fb3d8-d56e-4215-b746-05fcbd02d30e",
          batch_id: "61cb719a-6432-4381-911e-d4447ecf6332",
          title: "Nouvelle campagne MuddyWater",
          summary: "Une publication technique propose des IOC.",
          novelty: "Nouvelle chaîne d’infection",
          technical_potential: 4,
          event_date: "2026-07-02",
          uncertainties: ["Attribution non vérifiée"],
          relevance_reasons: ["Rapport technique original"],
          actors: ["MuddyWater"],
          campaigns: [],
          malware: ["ExampleRAT"],
          cves: [],
          victims: [],
          sectors: ["gouvernement"],
          countries: ["Iran"],
          likely_artifacts: ["ioc", "configurations"],
          provisional_ioc_count: 1,
          provisional_ioc_type_counts: { ipv4: 1 },
          has_publisher_ioc_count: true,
          provisional_iocs: [
            {
              id: "9e0fa012-1a63-4c70-888a-3cbc7f3190d6",
              raw_value: "192.0.2.1",
              normalized_value: "192.0.2.1",
              declared_type: "ipv4",
              proposed_type: "ipv4",
              status: "provisional_visible",
              publication_refs: ["P1"],
              warnings: [],
            },
          ],
          editorial_status: "proposed",
          sources: [
            {
              id: "c6c38491-e0a3-4315-a64e-e27946a350a4",
              url: "https://vendor.example/report",
              canonical_url: "https://vendor.example/report",
              title: "Rapport technique original",
              publisher: "Vendor Research",
              role: "primary",
              published_at: "2026-07-10",
              event_date: "2026-07-02",
              citation: "Citation du modèle",
              verification_status: "unverified",
              verification_changed_at: null,
              verification_changed_by: null,
            },
          ],
        },
      ],
      total: 1,
      warning: "Propositions non vérifiées",
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url.includes("/discovery/candidates"))
        return Response.json(candidateResult);
      if (url.includes("/editorial-groups"))
        return Response.json(emptyEditorialBoard);
      if (url.endsWith("/discovery") && init?.method === "POST") {
        return Response.json(
          {
            job_id: "20658589-a6d5-4af5-b026-d5c6fcb3b7f0",
            status: "queued",
            reused: false,
          },
          { status: 202 },
        );
      }
      if (url.includes("/api/jobs/")) {
        return Response.json({
          id: "20658589-a6d5-4af5-b026-d5c6fcb3b7f0",
          kind: "discover_edition",
          aggregate_type: "edition",
          aggregate_id: iranEdition.id,
          status: "succeeded",
          progress_current: 4,
          progress_total: 4,
          user_message: "Candidats proposés — vérification humaine requise",
          attempt: 1,
          max_attempts: 1,
          next_retry_at: null,
          started_at: "2026-08-10T10:00:00Z",
          finished_at: "2026-08-10T10:01:00Z",
          heartbeat_at: "2026-08-10T10:01:00Z",
          error_code: null,
          error_message: null,
          correlation_id: "test",
          output_reference: "discovery-batch://batch",
          cancellation_requested: false,
          created_at: "2026-08-10T10:00:00Z",
          updated_at: "2026-08-10T10:01:00Z",
        });
      }
      if (url.endsWith(iranEdition.id)) return Response.json(iranEdition);
      return Response.json({ items: [], total: 0, page: 1, page_size: 20 });
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/editions/${iranEdition.id}`);
    const user = userEvent.setup();
    renderApp();

    const technicalDetails = await screen.findByText(
      "Détails techniques de la découverte",
    );
    expect(
      await screen.findByRole("heading", {
        name: "Nouvelle campagne MuddyWater",
        hidden: true,
      }),
    ).not.toBeVisible();
    expect(await screen.findByText("Citation orpheline")).not.toBeVisible();
    await user.click(technicalDetails);
    expect(
      screen.getByRole("heading", {
        name: "Nouvelle campagne MuddyWater",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(
        /Les métadonnées et comptes IOC de découverte sont provisoires/,
      )[0],
    ).toBeInTheDocument();
    expect(screen.getByText("Attribution non vérifiée")).toBeInTheDocument();
    expect(screen.getByText(/primary · unverified/)).toBeInTheDocument();
    expect(
      screen.getAllByText(
        /IOC repérés pendant la recherche — non encore vérifiés depuis les sources/,
      )[0],
    ).toBeInTheDocument();
    expect(
      screen.getByText(/ipv4: 1.*total éditeur annoncé/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Exemples : 192\.0\.2\.1/)).toBeInTheDocument();
    await user.click(screen.getByText("Voir la liste complète (1)"));
    expect(screen.getByText("192.0.2.1")).toBeInTheDocument();
    await user.click(screen.getByText(/Rapport et diagnostic/));
    expect(screen.getByText("Citation orpheline")).toBeVisible();
    expect(
      screen.getByText("Iran APT July 2026 technical report"),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Rechercher les sujets" }),
    );
    expect(await screen.findByText("Terminée")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input, init]) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        return url.endsWith("/discovery") && init?.method === "POST";
      }),
    ).toBe(true);
  });
});
