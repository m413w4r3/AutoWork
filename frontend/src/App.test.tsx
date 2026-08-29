import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { Edition } from "./api/editions";
import { withProductionNotStarted } from "./test-utils/fetchStubs";

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

/** Backend minimal pour une édition sans découverte ni job en cours. */
function discoveryFetchMock() {
  return vi.fn(
    withProductionNotStarted((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url.endsWith("/discovery/import/preview")) {
        return Response.json({
          sha256: "b".repeat(64),
          subject_count: 2,
          publication_count: 3,
          ioc_count: 5,
          ioc_type_counts: { ipv4: 5 },
          subjects: ["Campagne A", "Campagne B"],
          warnings: ["Avertissement parser"],
        });
      }
      if (url.endsWith("/discovery/import/confirm")) {
        return Response.json({
          batch_id: "9e2f4a1c-1d2b-4a3f-8c5e-6a7b8c9d0e1f",
          reused: false,
          source_mode: "manual_import",
          subject_count: 2,
          publication_count: 3,
        });
      }
      if (url.includes("/discovery/candidates")) {
        return Response.json({
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
        });
      }
      if (url.includes("/editorial-groups"))
        return Response.json(emptyEditorialBoard);
      if (url.endsWith(iranEdition.id)) return Response.json(iranEdition);
      void init;
      return Response.json({ items: [], total: 0, page: 1, page_size: 20 });
    }),
  );
}

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
      vi.fn(
        withProductionNotStarted(() =>
          Response.json({
            items: [iranEdition],
            total: 1,
            page: 1,
            page_size: 20,
          }),
        ),
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

  it("crée une édition Iran et affiche les actions de workflow autorisées", async () => {
    const fetchMock = vi.fn(
      withProductionNotStarted(
        (input: RequestInfo | URL, init?: RequestInit) => {
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
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/editions/new");
    const user = userEvent.setup();
    renderApp();

    await user.type(screen.getByLabelText("Pays"), "Iran");
    await user.type(screen.getByLabelText("Code pays"), "IR");
    await user.type(screen.getByLabelText("Période"), "2026-07");
    await user.clear(screen.getByLabelText("Langues"));
    await user.type(screen.getByLabelText("Langues"), "fr,en,fa");
    expect(
      screen.getByText(
        "Ces objectifs ne limitent jamais la sélection éditoriale.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText(
        "Objectif indicatif d’articles principaux — sans limite de sélection",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText(
        "Objectif indicatif de brèves — sans limite de sélection",
      ),
    ).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Profil de sources"));
    await user.type(screen.getByLabelText("Profil de sources"), "iran-default");
    await user.click(screen.getByRole("button", { name: "Créer l’édition" }));

    expect(
      await screen.findByRole("heading", { name: "Iran" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Démarrer la découverte" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Archiver l’édition" }),
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
      vi.fn(
        withProductionNotStarted(() =>
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
    const fetchMock = vi.fn(
      withProductionNotStarted((input: RequestInfo | URL) => {
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
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/editions/${iranEdition.id}`);

    renderApp();

    expect(await screen.findByText("Terminée")).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "Sujets candidats" }),
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
          incomplete_sources: [
            {
              id: "5b02f2c6-7a8e-4c0f-9f8e-3a2b4e6f1d90",
              title: "Publication incomplète",
              publisher: "unknown",
              raw_url: null,
              local_ref: "P2",
              published_at: null,
              period_relation: "unknown",
              role: "unknown",
              ioc_presence: "unknown",
              ioc_declared_count: null,
              ioc_visible_count: null,
              parsing_warnings: ["publication P2: no_explicit_url"],
            },
          ],
        },
      ],
      total: 1,
      warning: "Propositions non vérifiées",
    };
    const fetchMock = vi.fn(
      withProductionNotStarted(
        (input: RequestInfo | URL, init?: RequestInit) => {
          const url =
            typeof input === "string"
              ? input
              : input instanceof URL
                ? input.href
                : input.url;
          if (
            url.includes("/incomplete-sources/") &&
            init?.method === "PATCH"
          ) {
            return Response.json({
              source: {
                id: "5b02f2c6-7a8e-4c0f-9f8e-3a2b4e6f1d90",
                url: "https://vendor.example/recovered",
                canonical_url: "https://vendor.example/recovered",
                raw_url: "https://vendor.example/recovered",
                local_ref: "P2",
                source_ref: "source-recovered",
                title: "Publication incomplète",
                publisher: "unknown",
                role: "unknown",
                published_at: null,
                event_date: null,
                citation: null,
                period_relation: "unknown",
                ioc_presence: "unknown",
                ioc_declared_count: null,
                ioc_visible_count: null,
                parsing_warnings: ["url_attached_manually"],
                verification_status: "unverified",
                relationship_status: "provisional",
                verification_changed_at: null,
                verification_changed_by: null,
              },
              updated_subject_ids: [candidateResult.candidates[0]!.id],
            });
          }
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
        },
      ),
    );
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

    expect(screen.getByText(/URL absente/)).toBeInTheDocument();
    const urlInput = screen.getByPlaceholderText("https://...");
    await user.type(urlInput, "https://vendor.example/recovered");
    await user.click(screen.getByRole("button", { name: "Associer le lien" }));
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input, init]) => {
          const url =
            typeof input === "string"
              ? input
              : input instanceof URL
                ? input.href
                : input.url;
          return (
            url.includes(
              "/incomplete-sources/5b02f2c6-7a8e-4c0f-9f8e-3a2b4e6f1d90",
            ) &&
            init?.method === "PATCH" &&
            init?.body ===
              JSON.stringify({ url: "https://vendor.example/recovered" })
          );
        }),
      ).toBe(true);
    });

    await user.click(screen.getByText(/Rapport et diagnostic/));
    expect(screen.getByText("Citation orpheline")).toBeVisible();
    expect(
      screen.getByText("Iran APT July 2026 technical report"),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Nouvelle recherche ChatGPT" }),
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

  it("ouvre le collage d’une réponse ChatGPT sans job ni recherche préalable", async () => {
    const fetchMock = discoveryFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/editions/${iranEdition.id}`);
    const user = userEvent.setup();
    renderApp();

    await user.click(
      await screen.findByRole("button", { name: "Coller une réponse ChatGPT" }),
    );

    // Ce test protège la régression : l'ancien bouton disparaissait sans rien
    // afficher parce que le formulaire dépendait d'un jobId inexistant.
    expect(await screen.findByLabelText("Réponse ChatGPT")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Nouvelle recherche ChatGPT" }),
    ).toBeInTheDocument();
  });

  it("prévisualise puis confirme un import et rafraîchit découverte et chemin de fer", async () => {
    const fetchMock = discoveryFetchMock();
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/editions/${iranEdition.id}`);
    const user = userEvent.setup();
    renderApp();

    await user.click(
      await screen.findByRole("button", { name: "Coller une réponse ChatGPT" }),
    );
    await user.type(
      await screen.findByLabelText("Réponse ChatGPT"),
      "# SUJETS CANDIDATS",
    );
    await user.click(screen.getByRole("button", { name: "Prévisualiser" }));

    expect(
      await screen.findByText(/2 sujets · 3 publications · 5 IOC provisoires/),
    ).toBeInTheDocument();
    expect(await screen.findByText("Avertissement parser")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Confirmer et intégrer" }),
    );

    await waitFor(() => {
      expect(
        screen.queryByLabelText("Réponse ChatGPT"),
      ).not.toBeInTheDocument();
    });
    const urls = fetchMock.mock.calls.map(([input]) =>
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url,
    );
    expect(urls.some((url) => url.endsWith("/discovery/import/preview"))).toBe(
      true,
    );
    expect(urls.some((url) => url.endsWith("/discovery/import/confirm"))).toBe(
      true,
    );
  });

  it("attend la réconciliation d'un import avant de rafraîchir la sélection des sujets", async () => {
    // Régression : un import Markdown déclenche un job de réconciliation
    // asynchrone côté backend. Rafraîchir immédiatement (sans l'attendre)
    // faisait la course avec ce job et laissait la sélection des sujets
    // vide, notamment quand l'import n'apporte qu'un seul candidat.
    const reconciliationJobId = "8f14e45f-ceea-467e-88bb-7c31f5d59c37";
    const consolidatedCandidate = {
      id: "cand-1",
      batch_id: "9e2f4a1c-1d2b-4a3f-8c5e-6a7b8c9d0e1f",
      title: "Campagne consolidée",
      summary: "Résumé.",
      novelty: "Nouveau.",
      technical_potential: 2,
      event_date: null,
      uncertainties: [],
      relevance_reasons: [],
      actors: [],
      campaigns: [],
      malware: [],
      cves: [],
      victims: [],
      sectors: [],
      countries: [],
      likely_artifacts: [],
      iocs: [],
      editorial_status: "proposed",
      sources: [],
      incomplete_sources: [],
      local_ref: "S1",
      actor_or_campaign: "unknown",
      technical_potential_reason: "n/a",
      parsing_warnings: [],
      context_only: false,
      selectable: false,
      valid_publication_count: 0,
      incomplete_publication_count: 0,
    };
    let candidatesCallCount = 0;
    const fetchMock = vi.fn(
      withProductionNotStarted((input: RequestInfo | URL) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        if (url.endsWith("/discovery/import/preview")) {
          return Response.json({
            sha256: "b".repeat(64),
            subject_count: 1,
            publication_count: 0,
            ioc_count: 0,
            ioc_type_counts: {},
            subjects: ["Campagne consolidée"],
            warnings: [],
          });
        }
        if (url.endsWith("/discovery/import/confirm")) {
          return Response.json({
            batch_id: "9e2f4a1c-1d2b-4a3f-8c5e-6a7b8c9d0e1f",
            reused: false,
            source_mode: "manual_import",
            subject_count: 1,
            publication_count: 0,
            reconciliation_job_id: reconciliationJobId,
          });
        }
        if (url.includes(`/api/jobs/${reconciliationJobId}`)) {
          return Response.json({
            id: reconciliationJobId,
            kind: "reconcile_discovery",
            aggregate_type: "edition",
            aggregate_id: iranEdition.id,
            status: "succeeded",
            progress_current: 1,
            progress_total: 1,
            user_message: null,
            attempt: 1,
            max_attempts: 3,
            next_retry_at: null,
            started_at: "2026-08-10T10:00:00Z",
            finished_at: "2026-08-10T10:01:00Z",
            heartbeat_at: "2026-08-10T10:01:00Z",
            error_code: null,
            error_message: null,
            error_details: null,
            correlation_id: "reconcile-test",
            output_reference: null,
            cancellation_requested: false,
            created_at: "2026-08-10T10:00:00Z",
            updated_at: "2026-08-10T10:01:00Z",
          });
        }
        if (url.includes("/discovery/candidates")) {
          candidatesCallCount += 1;
          // Le premier appel a lieu avant la réconciliation : rien de
          // consolidé pour l'instant, comme le backend le renverrait tant
          // que le job n'a pas tourné.
          const consolidated = candidatesCallCount > 1;
          return Response.json({
            batches: [],
            candidates: consolidated ? [consolidatedCandidate] : [],
            total: consolidated ? 1 : 0,
            merge_stats: {
              raw_batch_count: 1,
              raw_candidate_count: 1,
              consolidated_candidate_count: consolidated ? 1 : 0,
              unique_publication_count: 0,
              duplicate_publication_occurrence_count: 0,
            },
            warning: "",
          });
        }
        if (url.includes("/editorial-groups"))
          return Response.json(emptyEditorialBoard);
        if (url.endsWith(iranEdition.id)) return Response.json(iranEdition);
        return Response.json({ items: [], total: 0, page: 1, page_size: 20 });
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", `/editions/${iranEdition.id}`);
    const user = userEvent.setup();
    renderApp();

    await user.click(
      await screen.findByRole("button", { name: "Coller une réponse ChatGPT" }),
    );
    await user.type(
      await screen.findByLabelText("Réponse ChatGPT"),
      "# SUJETS CANDIDATS",
    );
    await user.click(screen.getByRole("button", { name: "Prévisualiser" }));
    await screen.findByText(/1 sujets · 0 publications · 0 IOC provisoires/);
    await user.click(
      screen.getByRole("button", { name: "Confirmer et intégrer" }),
    );

    // Le job de réconciliation est suivi (comme une recherche ChatGPT) au
    // lieu d'être ignoré.
    expect(await screen.findByText("Terminée")).toBeInTheDocument();
    // Et la sélection des sujets ne se met à jour qu'une fois ce job
    // terminal, en montrant le sujet consolidé.
    expect(
      await screen.findByRole("heading", { name: "Sujets candidats" }),
    ).toBeInTheDocument();
    expect(candidatesCallCount).toBeGreaterThan(1);
    expect(
      fetchMock.mock.calls.some(([input]) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        return url.includes(`/api/jobs/${reconciliationJobId}`);
      }),
    ).toBe(true);
  });
});
