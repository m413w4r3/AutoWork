import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobStatusCard } from "./JobStatusCard";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("JobStatusCard", () => {
  it("utilise le suivi HTTP périodique quand EventSource est indisponible", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("EventSource", undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            id: "4de4af61-811e-4c1c-ad4a-9b39a5c06c94",
            kind: "demo.deterministic",
            aggregate_type: "subject",
            aggregate_id: "b131b279-d486-4af2-a1b8-c3579583b97e",
            status: "running",
            progress_current: 2,
            progress_total: 4,
            user_message: "Étape contrôlée",
            attempt: 1,
            max_attempts: 3,
            next_retry_at: null,
            started_at: "2026-08-07T10:00:00Z",
            finished_at: null,
            heartbeat_at: "2026-08-07T10:00:01Z",
            error_code: null,
            error_message: null,
            correlation_id: "test-correlation",
            output_reference: null,
            cancellation_requested: false,
            created_at: "2026-08-07T10:00:00Z",
            updated_at: "2026-08-07T10:00:01Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <JobStatusCard jobId="4de4af61-811e-4c1c-ad4a-9b39a5c06c94" />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("En cours")).toBeInTheDocument();
    expect(screen.getByText("50 % — 2/4")).toBeInTheDocument();
    expect(screen.getByText("Étape contrôlée")).toBeInTheDocument();
    const cancel = screen.getByRole("button", { name: "Annuler la tâche" });
    expect(cancel).toBeEnabled();
    await user.click(cancel);
    expect(fetch).toHaveBeenCalledWith(
      "/api/jobs/4de4af61-811e-4c1c-ad4a-9b39a5c06c94/cancel",
      { method: "POST" },
    );
    expect(fetch).toHaveBeenCalledWith(
      "/api/jobs/4de4af61-811e-4c1c-ad4a-9b39a5c06c94",
    );
  });

  it("explique une erreur bridge transitoire et propose la relance", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("EventSource", undefined);
    const failed = {
      id: "4de4af61-811e-4c1c-ad4a-9b39a5c06c94",
      kind: "brief.generate",
      aggregate_type: "subject",
      aggregate_id: "b131b279-d486-4af2-a1b8-c3579583b97e",
      status: "failed",
      progress_current: 0,
      progress_total: 1,
      user_message: null,
      attempt: 1,
      max_attempts: 3,
      next_retry_at: null,
      started_at: "2026-08-07T10:00:00Z",
      finished_at: "2026-08-07T10:00:02Z",
      heartbeat_at: "2026-08-07T10:00:02Z",
      error_code: "bridge_extension_disconnected",
      error_message: "safe fallback",
      correlation_id: "diag-bridge-42",
      output_reference: null,
      cancellation_requested: false,
      created_at: "2026-08-07T10:00:00Z",
      updated_at: "2026-08-07T10:00:02Z",
    };
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const body = url.endsWith("/retry")
        ? { ...failed, status: "queued" }
        : failed;
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <JobStatusCard jobId={failed.id} />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText(/L’extension Chrome est déconnectée/),
    ).toHaveTextContent("erreur transitoire");
    expect(screen.getByText(/diag-bridge-42/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Réessayer" }));
    expect(fetchMock).toHaveBeenCalledWith(`/api/jobs/${failed.id}/retry`, {
      method: "POST",
    });
  });

  it("affiche le diagnostic structuré et relance uniquement la structuration", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("EventSource", undefined);
    const retryStructuring = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            id: "4de4af61-811e-4c1c-ad4a-9b39a5c06c94",
            kind: "discover_edition",
            aggregate_type: "edition",
            aggregate_id: "b131b279-d486-4af2-a1b8-c3579583b97e",
            status: "failed",
            progress_current: 3,
            progress_total: 4,
            user_message: null,
            attempt: 1,
            max_attempts: 1,
            next_retry_at: null,
            started_at: "2026-08-07T10:00:00Z",
            finished_at: "2026-08-07T10:00:02Z",
            heartbeat_at: "2026-08-07T10:00:02Z",
            error_code: "discovery_structuring_invalid",
            error_message: "JSON invalide",
            error_details: {
              phase: "json_parse",
              validation_kind: "json_invalid",
              valid_count: 1,
              rejected_count: 2,
              model_run_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
              research_model_run_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
              diagnostic_available: true,
              can_retry_structuring: true,
            },
            correlation_id: "diag-structure-42",
            output_reference: null,
            cancellation_requested: false,
            created_at: "2026-08-07T10:00:00Z",
            updated_at: "2026-08-07T10:00:02Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <JobStatusCard
          jobId="4de4af61-811e-4c1c-ad4a-9b39a5c06c94"
          onRetryStructuring={retryStructuring}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("json_parse")).toBeInTheDocument();
    expect(screen.getByText("1 valides · 2 rejetés")).toBeInTheDocument();
    expect(screen.getByText("disponible")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Retenter la structuration" }),
    );
    expect(retryStructuring).toHaveBeenCalledWith(
      "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    );
  });
});
