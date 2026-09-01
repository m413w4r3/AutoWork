import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BatchStatus } from "../../api/production";
import { ProductionConsole } from "./ProductionConsole";
import { productionBatchPollingInterval } from "./productionPolling";

const EDITION_ID = "edition-1";

function renderConsole(batch: BatchStatus | null) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    void input;
    void init;
    return Promise.resolve(
      batch ? Response.json(batch) : new Response(null, { status: 404 }),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  render(
    <QueryClientProvider client={client}>
      <ProductionConsole editionId={EDITION_ID} />
    </QueryClientProvider>,
  );
  return { client, fetchMock };
}

afterEach(() => vi.unstubAllGlobals());

describe("ProductionConsole", () => {
  it("affiche le détail compact de l’extraction de l’article actif", async () => {
    const batch: BatchStatus = {
      batch_id: "batch-extraction",
      edition_id: EDITION_ID,
      status: "running",
      phase: "initial",
      next_dispatch_at: null,
      items: 1,
      completed: 0,
      needs_review: 0,
      failed: 0,
      cancelled: 0,
      item_details: [
        {
          position: 1,
          subject_id: "subject-extraction",
          title: "Article en extraction",
          run_id: "run-extraction",
          status: "running",
          current_stage: "extraction",
          pipeline_generation: 0,
          auto_recovery_count: 0,
          error_code: null,
          error_message: null,
          extraction_progress: {
            total_sources: 3,
            completed_sources: 2,
            full_total: 1,
            full_completed: 1,
            ioc_rules_total: 2,
            ioc_rules_completed: 1,
            cache_hits: 1,
            model_calls: 1,
            confirmed_iocs: 184,
            contextual_iocs: 12,
            rules_total: 5,
            yara_rules: 3,
            sigma_rules: 1,
            suricata_rules: 1,
            snort_rules: 0,
            active_source_id: "S3",
            active_source_title: "Third source",
            active_profile: "ioc_rules",
            sources: [
              {
                source_id: "S1",
                title: "First source",
                profile: "full",
                status: "succeeded",
                ioc_count: 100,
                rule_count: 3,
              },
              {
                source_id: "S2",
                title: "Second source",
                profile: "ioc_rules",
                status: "cached",
                ioc_count: 96,
                rule_count: 2,
              },
              {
                source_id: "S3",
                title: "Third source",
                profile: "ioc_rules",
                status: "running",
                ioc_count: 0,
                rule_count: 0,
              },
            ],
          },
        },
      ],
      created_at: "2026-08-29T10:00:00Z",
      started_at: "2026-08-29T10:00:00Z",
      finished_at: null,
    };

    renderConsole(batch);

    const progress = await screen.findByLabelText(
      "Progression de l’extraction",
    );
    expect(await screen.findByText("Extraction 2 / 3")).toBeInTheDocument();
    expect(screen.getByText("FULL 1 / 1")).toBeInTheDocument();
    expect(screen.getByText("IOC uniquement 1 / 2")).toBeInTheDocument();
    expect(progress).toHaveTextContent(
      "Active : S3 — Third source · IOC uniquement",
    );
    expect(progress).toHaveTextContent("IOCs : 184 confirmés · 12 contextuels");
    expect(progress).toHaveTextContent(
      "Règles : 5 · YARA 3 · Sigma 1 · Suricata 1 · Snort 0",
    );
    expect(progress).toHaveTextContent("Réutilisés : 1 · Appels modèle : 1");
    expect(screen.getByText("S2")).toBeInTheDocument();
    expect(progress).toHaveTextContent("Réutilisés");
    expect(progress).toHaveTextContent("En cours");
  });

  it("affiche la phase, les compteurs, les récupérations et les erreurs", async () => {
    const batch: BatchStatus = {
      batch_id: "batch-1",
      edition_id: EDITION_ID,
      status: "running",
      phase: "recovery",
      next_dispatch_at: new Date(Date.now() + 42_000).toISOString(),
      items: 4,
      completed: 1,
      needs_review: 1,
      failed: 1,
      cancelled: 1,
      item_details: [
        {
          position: 1,
          subject_id: "subject-1",
          title: "Sujet prêt",
          run_id: "run-1",
          status: "ready",
          current_stage: "assembly",
          pipeline_generation: 7,
          auto_recovery_count: 0,
          error_code: null,
          error_message: null,
        },
        {
          position: 2,
          subject_id: "subject-2",
          title: "Sujet à vérifier",
          run_id: "run-2",
          status: "needs_review",
          current_stage: "synthesis",
          pipeline_generation: 8,
          auto_recovery_count: 1,
          error_code: "review_required",
          error_message: "Validation manuelle requise.",
        },
        {
          position: 3,
          subject_id: "subject-3",
          title: "Sujet en échec",
          run_id: "run-3",
          status: "failed",
          current_stage: "synthesis",
          pipeline_generation: 8,
          auto_recovery_count: 2,
          error_code: "synthesis_failed",
          error_message: "La synthèse a échoué.",
        },
        {
          position: 4,
          subject_id: "subject-4",
          title: "Sujet annulé",
          run_id: "run-4",
          status: "cancelled",
          current_stage: "sources",
          pipeline_generation: 7,
          auto_recovery_count: 0,
          error_code: null,
          error_message: null,
        },
      ],
      created_at: "2026-08-29T10:00:00Z",
      started_at: "2026-08-29T10:00:00Z",
      finished_at: null,
    };

    renderConsole(batch);

    expect(
      await screen.findByRole("heading", { name: "4 / 4 articles traités" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Récupération automatique")).toBeInTheDocument();
    expect(screen.getByLabelText("Compteurs de production")).toHaveTextContent(
      "1 prêts",
    );
    expect(screen.getByLabelText("Compteurs de production")).toHaveTextContent(
      "1 à vérifier",
    );
    expect(screen.getByLabelText("Compteurs de production")).toHaveTextContent(
      "1 échecs",
    );
    expect(screen.getByLabelText("Compteurs de production")).toHaveTextContent(
      "1 annulés",
    );
    expect(screen.getByText("1 récupération automatique")).toBeInTheDocument();
    expect(screen.queryByText(/Génération/)).not.toBeInTheDocument();
    expect(
      screen.getByText("Validation manuelle requise."),
    ).toBeInTheDocument();
    expect(screen.getByText(/synthesis_failed/)).toBeInTheDocument();
    expect(
      screen.queryByText("détails internes interdits"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sujet prêt" })).toHaveAttribute(
      "href",
      "/subjects/subject-1",
    );
    expect(
      screen.getByText(/Démarrage du prochain article dans 00:4[12]/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Arrêter le lot" }),
    ).toBeInTheDocument();
  });

  it("ne présente pas une étape active pendant le délai avant le prochain article", async () => {
    const batch: BatchStatus = {
      batch_id: "batch-pacing",
      edition_id: EDITION_ID,
      status: "running",
      phase: "initial",
      next_dispatch_at: new Date(Date.now() + 42_000).toISOString(),
      items: 2,
      completed: 1,
      needs_review: 0,
      failed: 0,
      cancelled: 0,
      item_details: [
        {
          position: 1,
          subject_id: "subject-done",
          title: "Article terminé",
          run_id: "run-done",
          status: "ready",
          current_stage: "assembly",
          pipeline_generation: 0,
          auto_recovery_count: 0,
          error_code: null,
          error_message: null,
        },
        {
          position: 2,
          subject_id: "subject-next",
          title: "Article suivant",
          run_id: "run-next",
          status: "running",
          current_stage: "sources",
          pipeline_generation: 0,
          auto_recovery_count: 0,
          error_code: null,
          error_message: null,
        },
      ],
      created_at: "2026-08-29T10:00:00Z",
      started_at: "2026-08-29T10:00:00Z",
      finished_at: null,
    };

    renderConsole(batch);

    expect(await screen.findByText("Démarrage planifié")).toBeInTheDocument();
    expect(screen.getByText("En attente du démarrage")).toBeInTheDocument();
    expect(screen.queryByText("Étape : Sources")).not.toBeInTheDocument();
  });

  it("invalide l’édition une seule fois quand le lot est terminal", async () => {
    const batch: BatchStatus = {
      batch_id: "batch-2",
      edition_id: EDITION_ID,
      status: "completed_with_issues",
      phase: "review",
      next_dispatch_at: null,
      items: 0,
      completed: 0,
      needs_review: 0,
      failed: 0,
      cancelled: 0,
      item_details: [],
      created_at: "2026-08-29T10:00:00Z",
      started_at: "2026-08-29T10:00:00Z",
      finished_at: "2026-08-29T10:01:00Z",
    };
    const { client, fetchMock } = renderConsole(batch);
    const invalidate = vi.spyOn(client, "invalidateQueries");

    await screen.findByRole("heading", { name: "0 / 0 articles traités" });
    expect(
      screen.queryByRole("button", { name: "Arrêter le lot" }),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ["edition", EDITION_ID],
      }),
    );
    expect(
      invalidate.mock.calls.filter(
        ([filters]) =>
          filters?.queryKey?.[0] === "edition" &&
          filters?.queryKey?.[1] === EDITION_ID,
      ),
    ).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("arrête le lot exact et invalide le batch, l’édition et la revue", async () => {
    const batch: BatchStatus = {
      batch_id: "batch-stop",
      edition_id: EDITION_ID,
      status: "running",
      phase: "initial",
      next_dispatch_at: null,
      items: 1,
      completed: 0,
      needs_review: 0,
      failed: 0,
      cancelled: 0,
      item_details: [],
      created_at: "2026-08-29T10:00:00Z",
      started_at: "2026-08-29T10:00:00Z",
      finished_at: null,
    };
    const { client, fetchMock } = renderConsole(batch);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Arrêter le lot" }),
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
      ).toBe(true),
    );
    const post = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(post?.[0]).toBe(
      `/api/editions/${EDITION_ID}/production/${batch.batch_id}/cancel`,
    );
    await waitFor(() => {
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ["batch", EDITION_ID],
      });
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ["edition", EDITION_ID],
      });
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ["edition-review", EDITION_ID],
      });
    });
  });

  it.each([
    ["queued", 2_000],
    ["running", 2_000],
    ["completed", false],
    ["completed_with_issues", false],
    ["cancelled", false],
  ] as const)("polling %s => %s", (status, expected) => {
    expect(productionBatchPollingInterval(status)).toBe(expected);
  });
});
