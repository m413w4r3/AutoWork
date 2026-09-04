import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ExtractionProgress } from "../api/production";
import { shouldPollProduction } from "../api/production";
import { SubjectProduction } from "./SubjectProduction";

const SUBJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const ATTACHMENT_A_URL =
  "https://www.whisper.security/attachment-a-reserve-domains.csv";
const ATTACHMENT_B_URL =
  "https://www.whisper.security/attachment-b-live-server.csv";

function extractionProgress(
  sources: ExtractionProgress["sources"],
): ExtractionProgress {
  return {
    total_sources: sources.length,
    completed_sources: sources.filter((source) =>
      ["cached", "succeeded"].includes(source.status),
    ).length,
    full_total: sources.filter((source) => source.profile === "full").length,
    full_completed: 0,
    ioc_rules_total: sources.filter((source) => source.profile === "ioc_rules")
      .length,
    ioc_rules_completed: 0,
    cache_hits: sources.filter((source) => source.status === "cached").length,
    model_calls: 0,
    confirmed_iocs: 0,
    contextual_iocs: 0,
    rules_total: 0,
    yara_rules: 0,
    sigma_rules: 0,
    suricata_rules: 0,
    snort_rules: 0,
    active_source_id: null,
    active_source_title: null,
    active_profile: null,
    sources,
  };
}

function renderProduction() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SubjectProduction subjectId={SUBJECT_ID} />
    </QueryClientProvider>,
  );
}

function status(
  runStatus: "queued" | "running" | "ready" | "needs_review" | "failed",
  currentStage = "extraction",
) {
  return {
    subject_id: SUBJECT_ID,
    title: "TAG-182 et MarkiRAT",
    status: runStatus,
    current_stage: currentStage,
    progress_current: 2,
    progress_total: 5,
    references_conversation_id: "c-1",
    synthesis_conversation_id: null,
    run_id: "r-1",
    pipeline_generation: 3,
    created_at: "2026-08-10T10:00:00Z",
    started_at: "2026-08-10T10:00:00Z",
    finished_at: runStatus === "running" ? null : "2026-08-10T10:00:00Z",
    error_code: null,
    error_message: null,
    error_details: null,
    warnings: [],
    stages: {
      sources: {
        status: "succeeded",
        version: null,
        error_code: null,
        error_message: null,
      },
      references: {
        status: "succeeded",
        version: null,
        error_code: null,
        error_message: null,
      },
      extraction: {
        status:
          runStatus === "failed"
            ? "failed"
            : runStatus === "needs_review"
              ? "needs_review"
              : "succeeded",
        version: null,
        error_code:
          runStatus === "failed"
            ? "extraction_failed"
            : runStatus === "needs_review"
              ? "needs_review_code"
              : null,
        error_message: null,
      },
      synthesis: {
        status: "pending",
        version: null,
        error_code: null,
        error_message: null,
      },
      assembly: {
        status: "pending",
        version: null,
        error_code: null,
        error_message: null,
      },
    },
    recovery_disposition: "auto",
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("SubjectProduction retry from stage", () => {
  it.each(["failed", "needs_review"] as const)(
    "%s extraction affiche l’action prioritaire et appelle le retry générique",
    async (runStatus) => {
      const fetchMock = vi.fn(
        (_input: RequestInfo | URL, init?: RequestInit) =>
          init?.method === "POST"
            ? Promise.resolve(Response.json({ status: "running" }))
            : Promise.resolve(Response.json(status(runStatus))),
      );
      vi.stubGlobal("fetch", fetchMock);
      const user = userEvent.setup();
      renderProduction();
      expect(await screen.findByRole("alert")).toHaveTextContent("Extraction");
      expect(
        screen.getByRole("button", { name: "Relancer cette étape" }),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/toutes les étapes suivantes seront recalculées/),
      ).toBeInTheDocument();
      await user.click(
        screen.getByRole("button", { name: "Relancer cette étape" }),
      );
      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          `/api/subjects/${SUBJECT_ID}/production/retry`,
          expect.objectContaining({
            method: "POST",
            body: JSON.stringify({ stage: "extraction" }),
          }),
        ),
      );
    },
  );

  it("place le bloqueur Q2 avant le warning Attachment B", async () => {
    const warning = `supplemental_collection_failed:url=${ATTACHMENT_B_URL}:code=source_collection_no_success:blocked=1`;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...status("needs_review"),
          error_code: "q2_source_coverage_failed",
          error_message: "One or more Q1 sources could not be analysed",
          error_details: {
            failed_source_ids: ["S14"],
            source_failures: {
              S14: {
                source_url: ATTACHMENT_A_URL,
                error_code: "q2_source_unavailable",
              },
            },
          },
          warnings: [warning],
          extraction_progress: extractionProgress([
            {
              source_id: "S14",
              title: "attachment-a-reserve-domains.csv",
              profile: "full",
              status: "failed",
              ioc_count: 0,
              rule_count: 0,
            },
          ]),
        }),
      ),
    );
    renderProduction();

    const alert = await screen.findByRole("alert");
    const warningHeading = screen.getByRole("heading", {
      name: "Avertissements non bloquants",
    });
    const warningSection = warningHeading.closest("section");
    if (!warningSection) throw new Error("Warning section not found");

    expect(alert).toHaveTextContent("Problème bloquant");
    expect(alert).toHaveTextContent("attachment-a-reserve-domains.csv");
    expect(alert).toHaveTextContent(ATTACHMENT_A_URL);
    expect(alert).not.toHaveTextContent("attachment-b-live-server.csv");
    expect(
      alert.compareDocumentPosition(warningSection) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    const recovery = screen.getByRole("button", {
      name: "Relancer cette étape",
    });
    expect(
      alert.compareDocumentPosition(recovery) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      recovery.compareDocumentPosition(warningSection) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(warningSection).toHaveAttribute("role", "note");
    expect(warningSection).toHaveTextContent(
      "Source supplémentaire non archivée",
    );
    expect(warningSection).toHaveTextContent("attachment-b-live-server.csv");
    expect(warningSection).not.toHaveTextContent(warning);

    const diagnostics = screen.getByText(warning).closest("details");
    expect(diagnostics).toBeInTheDocument();
    expect(diagnostics).not.toHaveAttribute("open");
  });

  it("liste plusieurs sources bloquantes sans restituer le dump JSON", async () => {
    const sourceBUrl = "https://www.whisper.security/attachment-b.csv";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...status("failed"),
          error_code: "q2_source_coverage_failed",
          error_details: {
            failed_source_ids: ["S14", "S15"],
            source_failures: {
              S14: {
                source_url: ATTACHMENT_A_URL,
                error_code: "source_content_invalid",
              },
              S15: {
                source_url: sourceBUrl,
                error_code: "q2_source_unavailable",
              },
            },
          },
        }),
      ),
    );
    renderProduction();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("S14");
    expect(alert).toHaveTextContent(ATTACHMENT_A_URL);
    expect(alert).toHaveTextContent("S15");
    expect(alert).toHaveTextContent(sourceBUrl);
    expect(alert).not.toHaveTextContent('"source_failures"');
    expect(alert.querySelectorAll("li")).toHaveLength(2);
  });

  it("affiche un skip Q2 comme warning non bloquant", async () => {
    const progress = extractionProgress([
      {
        source_id: "S14",
        title: "attachment-a-reserve-domains.csv",
        profile: "full",
        status: "skipped",
        ioc_count: 0,
        rule_count: 0,
        skip: {
          source_url: ATTACHMENT_A_URL,
          reason_code: "live_unavailable_archive_unusable",
          blocking: false,
        },
      },
    ]);
    progress.skipped_sources = 1;
    progress.skipped_source_ids = ["S14"];
    progress.source_skips = {
      S14: progress.sources[0]?.skip ?? { blocking: false },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...status("running"),
          extraction_progress: progress,
        }),
      ),
    );
    renderProduction();

    expect(
      await screen.findByText("S14 — source ignorée pour l’extraction"),
    ).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(
      "aucune archive exploitable n’était disponible",
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("présente un succès avec le label d’archive de secours", async () => {
    const progress = extractionProgress([
      {
        source_id: "S14",
        title: "attachment-a-reserve-domains.csv",
        profile: "full",
        status: "succeeded",
        ioc_count: 1,
        rule_count: 0,
        access_mode: "archive_fallback",
      },
    ]);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...status("running"),
          extraction_progress: progress,
        }),
      ),
    );
    renderProduction();

    const fallback = await screen.findByText(/Archive de secours/);
    expect(fallback.closest("li")).toHaveClass("is-succeeded");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("ne plante pas avec des détails d’erreur nuls ou malformés", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...status("failed"),
          error_details: "unexpected legacy payload",
        }),
      ),
    );
    renderProduction();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Problème bloquant",
    );
  });

  it("failure terminale affiche le CTA avec la réserve de contrôle déterministe", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...status("failed"),
          recovery_disposition: "manual_only",
        }),
      ),
    );
    renderProduction();

    expect(await screen.findByRole("alert")).toHaveTextContent("Extraction");
    expect(
      screen.getByRole("button", { name: "Relancer cette étape" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/contrôle déterministe/)).toBeInTheDocument();
    expect(
      screen.getByText("Relancer depuis une étape précédente"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Relancer depuis Références" }),
    ).toBeInTheDocument();
  });

  it("permet de relancer l’assemblage en needs_review malgré manual_only", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "POST"
        ? Promise.resolve(Response.json({ status: "running" }))
        : Promise.resolve(
            Response.json({
              ...status("needs_review", "assembly"),
              recovery_disposition: "manual_only",
            }),
          ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderProduction();

    expect(await screen.findByRole("alert")).toHaveTextContent("Assemblage");
    const retryButton = screen.getByRole("button", {
      name: "Relancer cette étape",
    });
    expect(retryButton).toBeInTheDocument();
    await user.click(retryButton);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/subjects/${SUBJECT_ID}/production/retry`,
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ stage: "assembly" }),
        }),
      ),
    );
  });

  it("n’affiche pas l’étape courante dans le repli en plus du CTA principal", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...status("needs_review", "assembly"),
          recovery_disposition: "manual_only",
        }),
      ),
    );
    renderProduction();

    expect(
      await screen.findByRole("button", { name: "Relancer cette étape" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Relancer depuis Assemblage")).toHaveLength(1);
  });

  it("propose la récupération explicite sans afficher le retry générique", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(
          Response.json({
            production_run_id: "r-1",
            model_run_id: "m-1",
            stage: "extraction",
            pipeline_generation: 3,
            bridge_response_id: "bridge-1",
            submission_state: "submitted_or_unknown",
            phase: "reconciliation",
            text: "# réponse récupérée",
            sha256: "a".repeat(64),
            chars: 19,
            metadata: {},
            visible_available: true,
          }),
        );
      }
      return Promise.resolve(
        Response.json({
          ...status("needs_review"),
          error_code: "model_submission_reconciliation_required",
          reconciliation: {
            production_run_id: "r-1",
            model_run_id: "m-1",
            bridge_response_id: "bridge-1",
            submission_state: "submitted_or_unknown",
            phase: "reconciliation",
            stage: "extraction",
            pipeline_generation: 3,
            output_sha256: null,
            provenance: null,
            visible_available: true,
            batch_id: null,
          },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    renderProduction();
    expect(
      await screen.findByRole("heading", {
        name: "Récupérer la réponse ChatGPT",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Relancer cette étape" }),
    ).toBeNull();
    // No generic retry survives: the earlier-stage escape hatch included.
    expect(
      screen.queryByText("Relancer depuis une étape précédente"),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /^Relancer depuis / }),
    ).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("un run annulé appartenant à un lot n’offre pas de production isolée", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...status("failed"),
          status: "cancelled",
          batch_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        }),
      ),
    );
    renderProduction();
    expect(
      await screen.findByText(/appartient à une production d’édition/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Relancer la production" }),
    ).toBeNull();
  });

  it("un run annulé hors lot garde la relance standalone", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          Response.json({ ...status("failed"), status: "cancelled" }),
        ),
    );
    renderProduction();
    expect(
      await screen.findByRole("button", { name: "Relancer la production" }),
    ).toBeInTheDocument();
  });

  it("READY affiche un menu avec les cinq étapes", async () => {
    const readyBase = status("ready", "assembly");
    const ready = {
      ...readyBase,
      stages: Object.fromEntries(
        Object.entries(readyBase.stages).map(([key, value]) => [
          key,
          { ...value, status: "succeeded" },
        ]),
      ),
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(ready)));
    renderProduction();
    const select = await screen.findByRole("combobox", {
      name: "Relancer depuis une étape",
    });
    expect(select).toHaveValue("");
    expect(screen.getAllByRole("option")).toHaveLength(6);
  });

  it("RUNNING n’affiche aucune action retry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json(status("running"))),
    );
    renderProduction();
    await screen.findByText("TAG-182 et MarkiRAT");
    expect(screen.queryByText("Relancer depuis…")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Relancer cette étape" }),
    ).toBeNull();
  });

  it("RUNNING annule la tentative exacte exposée par le statut", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "POST"
        ? Promise.resolve(
            Response.json({
              action: "cancel",
              run_id: "r-1",
              status: "cancelled",
            }),
          )
        : Promise.resolve(Response.json(status("running"))),
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderProduction();
    await screen.findByText("TAG-182 et MarkiRAT");
    await user.click(screen.getByRole("button", { name: "Annuler" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/production/runs/r-1/cancel",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("après une relance refetch le nouveau run et reprend le polling", async () => {
    let reads = 0;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST")
        return Promise.resolve(
          Response.json({ status: "running", pipeline_generation: 4 }),
        );
      reads += 1;
      return Promise.resolve(
        Response.json(
          reads === 1
            ? status("ready", "assembly")
            : { ...status("running"), pipeline_generation: 4 },
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderProduction();
    await user.selectOptions(
      await screen.findByRole("combobox", {
        name: "Relancer depuis une étape",
      }),
      "extraction",
    );
    await waitFor(() =>
      expect(screen.getByText("en cours")).toBeInTheDocument(),
    );
    expect(shouldPollProduction("running")).toBe(true);
  });
});

it("polls only queued and running", () => {
  expect(shouldPollProduction("queued")).toBe(true);
  expect(shouldPollProduction("running")).toBe(true);
  expect(shouldPollProduction("ready")).toBe(false);
  expect(shouldPollProduction("failed")).toBe(false);
});
