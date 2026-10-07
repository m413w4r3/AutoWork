import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Edition } from "../../api/editions";
import type {
  BatchItemDetail,
  BatchStatus,
  CancelProductionBatchResponse,
  ProductionActivity,
  ProductionSubject,
} from "../../api/production";
import { ProductionConsole } from "./ProductionConsole";
import { productionBatchPollingInterval } from "../production/productionPolling";

const EDITION_ID = "edition-1";

function renderConsole(
  batch: BatchStatus | null,
  cancellation: CancelProductionBatchResponse = {
    action: "cancel",
    batch_id: batch?.batch_id ?? "batch-none",
    status: "cancelled",
    edition_state: "open",
    edition_version: 4,
  },
  subjects: ProductionSubject[] = [],
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const cachedEdition: Edition = {
    id: EDITION_ID,
    country: "France",
    country_code: "FR",
    period_start: "2026-08-01",
    period_end: "2026-08-31",
    tlp: "GREEN",
    languages: ["fr"],
    state: "open",
    version: 3,
    created_at: "2026-08-29T10:00:00Z",
    updated_at: "2026-08-29T10:00:00Z",
  };
  client.setQueryData(["edition", EDITION_ID], cachedEdition);
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    void input;
    if (init?.method === "POST")
      return Promise.resolve(Response.json(cancellation));
    return Promise.resolve(
      Response.json({
        edition_id: EDITION_ID,
        subjects,
        active_batch: batch,
        recent_batches: [],
      }),
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

function activityItem(
  activity: ProductionActivity,
  status: BatchItemDetail["status"] = "running",
): BatchItemDetail {
  return {
    position: 1,
    subject_id: "subject-activity",
    title: "Sujet activité",
    run_id: "run-activity",
    status,
    current_stage: activity.stage ?? "extraction",
    pipeline_generation: 0,
    auto_recovery_count: 0,
    error_code: null,
    error_message: null,
    activity,
  };
}

function activityBatch(item: BatchItemDetail): BatchStatus {
  return {
    batch_id: "batch-activity",
    edition_id: EDITION_ID,
    status: "running",
    phase: "initial",
    next_dispatch_at: null,
    items: 1,
    completed: 0,
    needs_review: 0,
    failed: 0,
    cancelled: 0,
    item_details: [item],
    created_at: "2026-10-07T11:00:00Z",
    started_at: "2026-10-07T11:00:00Z",
    finished_at: null,
  };
}

function activitySubject(
  subjectId: string,
  title: string,
  activity: ProductionActivity,
): ProductionSubject {
  return {
    subject_id: subjectId,
    title,
    tlp: "GREEN",
    latest_run_id: "run-activity",
    latest_run_number: 1,
    latest_status: "running",
    latest_stage: activity.stage,
    active_run_id: "run-activity",
    can_start: false,
    blocking_reason: "production_subject_active",
    activity,
  };
}

describe("ProductionConsole", () => {
  it("affiche l’état vide d’un board 200 sans lot", async () => {
    renderConsole(null);

    expect(
      await screen.findByText("Aucun lot de production n’est disponible."),
    ).toBeInTheDocument();
  });

  it("explique le sondage de réconciliation sans le présenter comme un démarrage modèle", async () => {
    renderConsole(
      activityBatch(
        activityItem(
          {
            kind: "reconciliation_probe",
            stage: "synthesis",
            started_at: "2026-10-07T11:58:00Z",
            since_seconds: 120,
            detail: "Sondage de réconciliation · aucune requête modèle",
            attempt: 1,
          },
          "needs_review",
        ),
      ),
    );

    expect(
      await screen.findByText(
        "Sondage de réconciliation programmé (aucune requête modèle)",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Démarrage planifié")).not.toBeInTheDocument();
  });

  it("affiche l’heure d’un nouvel essai programmé", async () => {
    const scheduledAt = new Date(2026, 9, 7, 14, 5).toISOString();
    renderConsole(
      activityBatch(
        activityItem({
          kind: "retry_scheduled",
          stage: "extraction",
          started_at: scheduledAt,
          since_seconds: 0,
          detail: "Nouvel essai programmé",
          attempt: 2,
        }),
      ),
    );

    expect(
      await screen.findByText(
        `Nouvel essai programmé à ${new Intl.DateTimeFormat("fr-FR", {
          hour: "2-digit",
          minute: "2-digit",
        }).format(new Date(scheduledAt))}`,
      ),
    ).toBeInTheDocument();
  });

  it("résume les appels, sondages et essais programmés au niveau du système", async () => {
    const subjects = [
      activitySubject("subject-model", "Sujet modèle", {
        kind: "model_call",
        stage: "synthesis",
        started_at: "2026-10-07T11:59:00Z",
        since_seconds: 60,
        detail: "Appel modèle en cours",
        attempt: 1,
      }),
      activitySubject("subject-probe", "Sujet sondage", {
        kind: "reconciliation_probe",
        stage: "synthesis",
        started_at: "2026-10-07T11:58:00Z",
        since_seconds: 120,
        detail: "Sondage de réconciliation · aucune requête modèle",
        attempt: 1,
      }),
      activitySubject("subject-retry", "Sujet nouvel essai", {
        kind: "retry_scheduled",
        stage: "extraction",
        started_at: new Date(2026, 9, 7, 14, 5).toISOString(),
        since_seconds: 0,
        detail: "Nouvel essai programmé",
        attempt: 2,
      }),
    ];
    renderConsole(null, undefined, subjects);

    expect(
      await screen.findByRole("heading", { name: "Activité du système" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Sujet modèle — Synthèse, appel modèle en cours depuis 1 min 0 s/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Sujet sondage — Sondage de réconciliation programmé/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Sujet nouvel essai — Nouvel essai programmé à/),
    ).toBeInTheDocument();
  });

  it("explique la réactivation manuelle d’un batch terminé", async () => {
    const batch = activityBatch(
      activityItem(
        {
          kind: "deterministic_stage",
          stage: "extraction",
          started_at: "2026-10-07T11:58:00Z",
          since_seconds: 120,
          detail: "Étape en cours",
          attempt: 1,
        },
        "running",
      ),
    );
    batch.reactivated_by_retry = true;
    renderConsole(batch);

    expect(
      await screen.findByText(
        "Ce lot a été réactivé par un nouvel essai manuel. Les articles déjà terminés restent comptabilisés.",
      ),
    ).toBeInTheDocument();
  });

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
          activity: {
            kind: "model_call",
            stage: "extraction",
            started_at: "2026-10-07T11:57:50Z",
            since_seconds: 130,
            detail: "Appel modèle en cours",
            attempt: 1,
          },
          extraction_progress: {
            total_sources: 3,
            completed_sources: 2,
            full_total: 1,
            full_completed: 1,
            ioc_rules_total: 2,
            ioc_rules_completed: 1,
            cache_hits: 1,
            model_calls: 1,
            current_source_id: "11111111-1111-4111-8111-111111111111",
            current_chunk_index: 7,
            confirmed_iocs: 184,
            contextual_iocs: 12,
            rules_total: 5,
            yara_rules: 3,
            sigma_rules: 1,
            suricata_rules: 1,
            snort_rules: 0,
            skipped_sources: 0,
            sources: [
              {
                source_id: "11111111-1111-4111-8111-111111111111",
                title: "First source",
                canonical_url: "https://first.example/report",
                tier: "core",
                profile: "full",
                status: "succeeded",
                reuse_state: "fresh",
                ioc_count: 100,
                rule_count: 3,
                chunks_done: 6,
                chunks_total: 14,
                chunks_total_is_estimate: true,
              },
              {
                source_id: "22222222-2222-4222-8222-222222222222",
                title: "Second source",
                canonical_url: "https://second.example/report",
                tier: "supporting",
                profile: "ioc_rules",
                status: "cached",
                reuse_state: "reused",
                ioc_count: 96,
                rule_count: 2,
              },
              {
                source_id: "33333333-3333-4333-8333-333333333333",
                title: "Third source",
                canonical_url: "https://third.example/report",
                tier: "technical",
                profile: "ioc_rules",
                status: "pending",
                reuse_state: null,
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
    expect(progress).toHaveTextContent("IOCs : 184 confirmés · 12 contextuels");
    expect(progress).toHaveTextContent(
      "Règles : 5 · YARA 3 · Sigma 1 · Suricata 1 · Snort 0",
    );
    expect(progress).toHaveTextContent(
      "Résultats existants : 1 · Appels modèle : 1",
    );
    expect(screen.getByText("Second source")).toBeInTheDocument();
    expect(progress).toHaveTextContent("Résultat existant");
    expect(progress).toHaveTextContent("En attente");
    expect(progress).toHaveTextContent("6 / 14 tranches (estimées)");
    expect(
      await screen.findByText(
        "Activité: Extraction — appel modèle en cours depuis 2 min 10 s — tranche 7/14 (estimée)",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Tranches terminées pour First source"),
    ).toHaveAttribute("max", "14");
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
      await screen.findByRole("heading", { name: "3 / 4 sujets traités" }),
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
      screen.getByRole("button", {
        name: "Annuler le lot de production",
      }),
    ).toBeInTheDocument();
  });

  it("ne compte pas les annulations comme du travail produit", async () => {
    const batch: BatchStatus = {
      batch_id: "batch-cancelled",
      edition_id: EDITION_ID,
      status: "cancelled",
      phase: "initial",
      next_dispatch_at: null,
      items: 2,
      completed: 0,
      needs_review: 0,
      failed: 0,
      cancelled: 2,
      item_details: [],
      created_at: "2026-08-29T10:00:00Z",
      started_at: "2026-08-29T10:00:00Z",
      finished_at: "2026-08-29T10:01:00Z",
    };

    renderConsole(batch);

    expect(
      await screen.findByRole("heading", { name: "0 / 2 sujets traités" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Compteurs de production")).toHaveTextContent(
      "2 annulés",
    );
    expect(screen.getByRole("progressbar")).toHaveValue(0);
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

    expect(
      await screen.findByText("En attente du prochain article"),
    ).toBeInTheDocument();
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

    await screen.findByRole("heading", { name: "0 / 0 sujets traités" });
    expect(
      screen.queryByRole("button", {
        name: "Annuler le lot de production",
      }),
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

  it("arrête le lot exact sans muter l’édition en cache", async () => {
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
      await screen.findByRole("button", {
        name: "Annuler le lot de production",
      }),
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
        queryKey: ["production-board", EDITION_ID],
      });
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ["edition", EDITION_ID],
      });
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ["edition-review", EDITION_ID],
      });
      // Cancelling a production batch does not alter Selection: the obsolete
      // editorial board invalidation must stay gone (AW-008 S08).
      expect(invalidate).not.toHaveBeenCalledWith({
        queryKey: ["editorial-board", EDITION_ID],
      });
      const cached = client.getQueryData<Edition>(["edition", EDITION_ID]);
      expect(cached?.state).toBe("open");
      expect(cached?.version).toBe(3);
    });
  });

  it("met en pause un lot actif et garde une action distincte pour l’annuler", async () => {
    const batch: BatchStatus = {
      batch_id: "batch-pause",
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
    const { fetchMock } = renderConsole(batch);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Mettre en pause" }),
    );

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/editions/${EDITION_ID}/production/${batch.batch_id}/pause`,
        { method: "POST" },
      ),
    );
    expect(
      screen.getByRole("button", { name: "Annuler le lot de production" }),
    ).toBeInTheDocument();
  });

  it("affiche l’étape en pause et reprend le même lot", async () => {
    const batch: BatchStatus = {
      batch_id: "batch-resume",
      edition_id: EDITION_ID,
      status: "paused",
      phase: "initial",
      next_dispatch_at: null,
      paused_at: "2026-08-29T10:02:00Z",
      paused_by: "dev-analyst",
      items: 1,
      completed: 0,
      needs_review: 0,
      failed: 0,
      cancelled: 0,
      item_details: [
        {
          position: 1,
          subject_id: "subject-paused",
          title: "Article en pause",
          run_id: "run-paused",
          status: "running",
          current_stage: "synthesis",
          paused: true,
          paused_stage: "synthesis",
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
    const { fetchMock } = renderConsole(batch);
    const user = userEvent.setup();

    expect(
      await screen.findByText("En pause à l’étape : Synthèse"),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("En pause");
    await user.click(screen.getByRole("button", { name: "Reprendre" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/editions/${EDITION_ID}/production/${batch.batch_id}/resume`,
        { method: "POST" },
      ),
    );
    expect(
      screen.getByRole("button", { name: "Annuler le lot de production" }),
    ).toBeInTheDocument();
  });

  it.each([
    ["queued", 2_000],
    ["running", 2_000],
    ["paused", 5_000],
    ["completed", false],
    ["completed_with_issues", false],
    ["cancelled", false],
  ] as const)("polling %s => %s", (status, expected) => {
    expect(productionBatchPollingInterval(status)).toBe(expected);
  });
});
