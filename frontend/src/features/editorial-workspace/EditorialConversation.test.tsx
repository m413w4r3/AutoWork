import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EditorialConversation } from "./EditorialConversation";
import type { EditorialThreadRead } from "../../api/editorialWorkspace";

const binding = {
  subject_id: "subject-1",
  production_run_id: "run-1",
  occurrence_id: "occurrence-1",
  artifact_id: "artifact-1",
  artifact_version: 3,
  canonical_sha256: "a".repeat(64),
};

function renderConversation(response: EditorialThreadRead) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify(response), {
          status: 200,
          headers: { "Content-Type": "application/json", ETag: response.etag },
        }),
      ),
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <EditorialConversation
        subjectId={binding.subject_id}
        occurrenceId={binding.occurrence_id}
        snapshotId="snapshot-1"
        targetLabel="Paragraphe synthétique"
      />
    </QueryClientProvider>,
  );
}

function threadResponse(read: EditorialThreadRead) {
  return new Response(JSON.stringify(read), {
    status: 200,
    headers: { "Content-Type": "application/json", ETag: read.etag },
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("EditorialConversation", () => {
  it("shows the unopened state without opening or writing a thread", async () => {
    renderConversation({
      kind: "unopened",
      binding,
      current: binding,
      bridge_status: "ready",
      event_sequence: 0,
      timeline: [],
      etag: '"thread-empty"',
    });

    expect(
      await screen.findByText(
        "Aucune discussion n’a encore été ouverte pour ce bloc.",
      ),
    ).toBeVisible();
    expect(
      screen.getByText("Discussion sur : Paragraphe synthétique"),
    ).toBeVisible();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders the append-only event timeline and origin version read-only", async () => {
    renderConversation({
      kind: "open",
      thread_id: "thread-1",
      origin: binding,
      current: { ...binding, artifact_version: 4, artifact_id: "artifact-2" },
      binding_state: "valid",
      bridge_status: "reply_available",
      event_sequence: 1,
      timeline: [
        {
          id: "event-1",
          sequence: 1,
          kind: "bridge_reply",
          actor_id: null,
          proposal_id: null,
          created_at: "2026-11-19T10:00:00Z",
          body: "Réponse synthétique de démonstration.",
        },
      ],
      etag: '"thread-one"',
    });

    await waitFor(() =>
      expect(
        screen.getByText("Réponse synthétique de démonstration."),
      ).toBeVisible(),
    );
    expect(screen.getByText("Version de référence du fil : v3")).toBeVisible();
    expect(
      screen.getByRole("heading", { name: /Réponse Bridge/ }),
    ).toBeVisible();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("polls while waiting for capacity and stops when the thread settles", async () => {
    vi.useFakeTimers();
    const ready = {
      kind: "open",
      thread_id: "thread-1",
      origin: binding,
      current: binding,
      binding_state: "valid",
      bridge_status: "ready",
      event_sequence: 0,
      timeline: [],
      etag: '"thread-ready"',
    } satisfies EditorialThreadRead;
    const waiting = {
      ...ready,
      bridge_status: "waiting_for_capacity",
      etag: '"thread-waiting"',
    } satisfies EditorialThreadRead;
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(threadResponse(waiting))
      .mockResolvedValueOnce(threadResponse(ready));
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <EditorialConversation
          subjectId={binding.subject_id}
          occurrenceId={binding.occurrence_id}
          snapshotId="snapshot-1"
          targetLabel="Paragraphe synthétique"
        />
      </QueryClientProvider>,
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_600);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const secondCall = fetchMock.mock.calls[1];
    if (!secondCall) throw new Error("waiting thread was not polled");
    const [secondRequest, secondInit] = secondCall;
    const secondUrl =
      typeof secondRequest === "string"
        ? secondRequest
        : secondRequest instanceof URL
          ? secondRequest.toString()
          : secondRequest.url;
    expect(secondUrl).toContain("after_sequence=0");
    expect(new Headers(secondInit?.headers).get("If-None-Match")).toBe(
      '"thread-waiting"',
    );
    expect(screen.getByText("La discussion est prête.")).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_200);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });
});
