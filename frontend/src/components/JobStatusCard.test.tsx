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
    const cancel = screen.getByRole("button", { name: "Annuler la collecte" });
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
});
