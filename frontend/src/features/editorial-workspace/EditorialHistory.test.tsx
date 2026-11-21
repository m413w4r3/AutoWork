import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EditorialHistoryItem } from "../../api/editorialWorkspace";
import { EditorialHistory } from "./EditorialHistory";

const subjectId = "11111111-1111-4111-8111-111111111111";

function historyItem(
  overrides: Partial<EditorialHistoryItem> = {},
): EditorialHistoryItem {
  return {
    proposal_id: "22222222-2222-4222-8222-222222222222",
    subject_id: subjectId,
    occurrence_id: "33333333-3333-4333-8333-333333333333",
    state: "accepted",
    origin: "manual_patch",
    operation: "rewrite_paragraph",
    operation_label: "Réécriture",
    actor_id: "44444444-4444-4444-8444-444444444444",
    actor_display: "Opérateur · 44444444",
    created_at: "2026-11-20T10:30:00Z",
    updated_at: "2026-11-20T10:31:00Z",
    canonical_result_artifact_id: "55555555-5555-4555-8555-555555555555",
    canonical_result_version: 7,
    canonical_result_stage: "synthesis",
    thread_id: null,
    thread_url: null,
    ...overrides,
  };
}

function renderHistory() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <EditorialHistory
        subjectId={subjectId}
        onClose={vi.fn()}
        onOpenThread={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EditorialHistory", () => {
  it("lists manual and Bridge proposals and loads the next page on request", async () => {
    const user = userEvent.setup();
    const bridge = historyItem({
      proposal_id: "66666666-6666-4666-8666-666666666666",
      occurrence_id: "77777777-7777-4777-8777-777777777777",
      origin: "bridge_draft",
      state: "proposed",
      operation: "create_table",
      operation_label: "Tableau",
      actor_display: "Opérateur · 88888888",
      canonical_result_artifact_id: null,
      canonical_result_version: null,
      canonical_result_stage: null,
      thread_id: "99999999-9999-4999-8999-999999999999",
      thread_url: `/api/subjects/${subjectId}/editor/threads/77777777-7777-4777-8777-777777777777`,
    });
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>((input) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        calls.push(url);
        const page =
          calls.length === 1
            ? { items: [historyItem()], next_cursor: "next-page", limit: 20 }
            : { items: [bridge], next_cursor: null, limit: 20 };
        return Promise.resolve(
          new Response(JSON.stringify(page), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );

    renderHistory();
    expect(await screen.findByText("Réécriture")).toBeVisible();
    expect(screen.getByText("Acceptée")).toBeVisible();
    expect(screen.getByText("Modification manuelle")).toBeVisible();
    expect(screen.getByText("Version canonique v7")).toBeVisible();
    expect(screen.getByText("Opérateur")).toBeVisible();
    expect(
      screen.getByText("44444444-4444-4444-8444-444444444444"),
    ).not.toBeVisible();

    await user.click(screen.getByRole("button", { name: "Charger plus" }));
    expect(await screen.findByText("Bridge")).toBeVisible();
    expect(screen.getByText("Tableau")).toBeVisible();
    expect(screen.getByText("En attente")).toBeVisible();
    expect(screen.getByText("Aucune nouvelle version canonique")).toBeVisible();
    const threadLink = screen.getByRole("link", {
      name: "Ouvrir le fil Bridge",
    });
    expect(threadLink).toHaveAttribute(
      "href",
      `/subjects/${subjectId}/editor?thread=77777777-7777-4777-8777-777777777777`,
    );
    expect(calls).toEqual([
      `/api/subjects/${subjectId}/editor/history?limit=20`,
      `/api/subjects/${subjectId}/editor/history?cursor=next-page&limit=20`,
    ]);
  });

  it("shows empty and read errors as recoverable states", async () => {
    const user = userEvent.setup();
    let shouldFail = false;
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() =>
        Promise.resolve(
          shouldFail
            ? new Response(
                JSON.stringify({
                  detail: {
                    code: "editorial_history_unavailable",
                    message: "L’historique est momentanément indisponible.",
                  },
                }),
                {
                  status: 503,
                  headers: { "Content-Type": "application/json" },
                },
              )
            : new Response(
                JSON.stringify({ items: [], next_cursor: null, limit: 20 }),
                {
                  status: 200,
                  headers: { "Content-Type": "application/json" },
                },
              ),
        ),
      ),
    );

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const view = render(
      <QueryClientProvider client={queryClient}>
        <EditorialHistory
          subjectId={subjectId}
          onClose={vi.fn()}
          onOpenThread={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(
      await screen.findByText("Aucune proposition dans l’historique."),
    ).toBeVisible();

    shouldFail = true;
    view.unmount();
    const failedClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    render(
      <QueryClientProvider client={failedClient}>
        <EditorialHistory
          subjectId={subjectId}
          onClose={vi.fn()}
          onOpenThread={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(
      await screen.findByText("L’historique est momentanément indisponible."),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Réessayer" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });
});
