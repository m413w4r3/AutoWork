import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EditorialConversation } from "./EditorialConversation";
import {
  sendEditorialThreadMessage,
  type EditorialThreadRead,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

const binding = {
  subject_id: "subject-1",
  production_run_id: "run-1",
  occurrence_id: "occurrence-1",
  artifact_id: "artifact-1",
  artifact_version: 3,
  canonical_sha256: "a".repeat(64),
};

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === "string"
    ? input
    : input instanceof URL
      ? input.href
      : input.url;
}

function renderConversation(
  response: EditorialThreadRead,
  onChangeTarget = vi.fn(),
) {
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
        onChangeTarget={onChangeTarget}
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
  vi.restoreAllMocks();
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
    expect(
      screen.getByRole("button", { name: "Envoyer un message" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: /proposition/i }),
    ).not.toBeInTheDocument();
  });

  it("offers a target-change action from the discussion header", async () => {
    const onChangeTarget = vi.fn();
    renderConversation(
      {
        kind: "unopened",
        binding,
        current: binding,
        bridge_status: "ready",
        event_sequence: 0,
        timeline: [],
        etag: '"thread-empty"',
      },
      onChangeTarget,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Changer de cible" }),
    );

    expect(onChangeTarget).toHaveBeenCalledOnce();
  });

  it("loads a referenced proposal from the current occurrence", async () => {
    const proposalId = "proposal-1";
    const proposal = {
      id: proposalId,
      subject_id: binding.subject_id,
      production_run_id: binding.production_run_id,
      occurrence_id: binding.occurrence_id,
      thread_id: "thread-1",
      state: "proposed",
      state_version: 1,
      operation: "rewrite_title",
      base_artifact_id: "artifact-1",
      base_artifact_version: 4,
      base_canonical_sha256: "a".repeat(64),
      patch_sha256: "b".repeat(64),
      patch: {
        schema_version: "editorial-patch-v1",
        operation: "rewrite_title",
        target: { kind: "title" },
        target_id: "target-1",
        occurrence_id: binding.occurrence_id,
        before_hash: "c".repeat(64),
        after: "Titre proposé dans le fil",
        rationale: "Raison synthétique",
      },
      diagnostics: {},
      created_at: "2026-11-19T12:00:00Z",
    };
    const thread: EditorialThreadRead = {
      kind: "open",
      thread_id: "thread-1",
      origin: binding,
      current: binding,
      binding_state: "valid",
      bridge_status: "ready",
      event_sequence: 1,
      timeline: [
        {
          id: "event-1",
          sequence: 1,
          kind: "proposal",
          actor_id: null,
          proposal_id: proposalId,
          created_at: "2026-11-19T12:00:00Z",
          body: null,
        },
      ],
      etag: '"thread-1"',
      previous_sessions: [],
    };
    const fetchMock = vi.fn<typeof fetch>((input) => {
      if (requestUrl(input).includes(`/proposals/${proposalId}`)) {
        return Promise.resolve(
          new Response(JSON.stringify(proposal), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }
      return Promise.resolve(threadResponse(thread));
    });
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
          targetLabel="Titre synthétique"
          onChangeTarget={vi.fn()}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Titre proposé dans le fil")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /proposer|accepter|rejeter/i }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        requestUrl(input).includes(
          `/threads/${binding.occurrence_id}/proposals/${proposalId}`,
        ),
      ),
    ).toBe(true);
  });

  it("keeps message drafts with their occurrence when switching before send", async () => {
    const postedMessages: Array<{
      url: string;
      occurrenceId: string;
      text: string;
    }> = [];
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const requestUrl =
        input instanceof Request
          ? input.url
          : input instanceof URL
            ? input.href
            : input;
      const url = new URL(requestUrl, "http://localhost");
      const occurrenceId = url.pathname.match(/\/threads\/([^/]+)/)?.[1];
      if (!occurrenceId) throw new Error("thread request lacks occurrence id");

      if (init?.method === "POST") {
        if (typeof init.body !== "string") {
          throw new Error("message POST body must be a string");
        }
        const payload = JSON.parse(init.body) as { text: string };
        postedMessages.push({
          url: url.pathname,
          occurrenceId,
          text: payload.text,
        });
        return Promise.resolve(
          new Response(
            JSON.stringify({
              kind: "accepted",
              thread_id: `thread-${occurrenceId}`,
              entry_id: `entry-${occurrenceId}`,
              generation: "queued",
            }),
            { status: 202, headers: { "Content-Type": "application/json" } },
          ),
        );
      }

      const targetBinding = { ...binding, occurrence_id: occurrenceId };
      return Promise.resolve(
        threadResponse({
          kind: "unopened",
          binding: targetBinding,
          current: targetBinding,
          bridge_status: "ready",
          event_sequence: 0,
          timeline: [],
          etag: `"${occurrenceId}-empty"`,
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });

    function SwitchableConversation() {
      const [occurrenceId, setOccurrenceId] = useState("occurrence-a");
      return (
        <>
          <button type="button" onClick={() => setOccurrenceId("occurrence-a")}>
            Sélectionner le bloc A
          </button>
          <button type="button" onClick={() => setOccurrenceId("occurrence-b")}>
            Sélectionner le bloc B
          </button>
          <EditorialConversation
            subjectId={binding.subject_id}
            occurrenceId={occurrenceId}
            snapshotId="snapshot-1"
            targetLabel={occurrenceId === "occurrence-a" ? "Bloc A" : "Bloc B"}
            onChangeTarget={() => undefined}
          />
        </>
      );
    }

    render(
      <QueryClientProvider client={client}>
        <SwitchableConversation />
      </QueryClientProvider>,
    );

    const textareaA = await screen.findByRole("textbox", {
      name: "Votre message à Bridge",
    });
    fireEvent.change(textareaA, { target: { value: "Brouillon du bloc A" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Sélectionner le bloc B" }),
    );

    const textareaB = await screen.findByRole("textbox", {
      name: "Votre message à Bridge",
    });
    expect(textareaB).toHaveValue("");
    fireEvent.change(textareaB, { target: { value: "Message du bloc B" } });
    fireEvent.click(screen.getByRole("button", { name: "Envoyer un message" }));

    await waitFor(() => expect(postedMessages).toHaveLength(1));
    expect(postedMessages[0]).toMatchObject({
      occurrenceId: "occurrence-b",
      text: "Message du bloc B",
    });
    expect(postedMessages[0]?.url).toContain("/threads/occurrence-b/entries");

    fireEvent.click(
      screen.getByRole("button", { name: "Sélectionner le bloc A" }),
    );
    expect(
      await screen.findByRole("textbox", { name: "Votre message à Bridge" }),
    ).toHaveValue("Brouillon du bloc A");
  });

  it("renders the append-only event timeline and origin version read-only", async () => {
    renderConversation({
      kind: "open",
      thread_id: "thread-1",
      origin: binding,
      current: { ...binding, artifact_version: 4, artifact_id: "artifact-2" },
      binding_state: "valid",
      previous_sessions: [],
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
    expect(
      screen.getByRole("log", { name: "Historique de la discussion" }),
    ).toHaveAttribute("aria-live", "polite");
    expect(
      screen.getByRole("button", { name: "Envoyer un message" }),
    ).toBeInTheDocument();
  });

  it("polls while waiting for capacity and stops when the thread settles", async () => {
    vi.useFakeTimers();
    const ready = {
      kind: "open",
      thread_id: "thread-1",
      origin: binding,
      current: binding,
      binding_state: "valid",
      previous_sessions: [],
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
          onChangeTarget={() => undefined}
        />
      </QueryClientProvider>,
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(
      screen.getByText(
        "Votre demande attend qu’une capacité Bridge soit disponible. Son état sera suivi dans ce fil.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /réessayer/i }),
    ).not.toBeInTheDocument();
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

  it("inserts suggestions for editing and sends on Enter but not Shift+Enter", async () => {
    const unopened: EditorialThreadRead = {
      kind: "unopened",
      binding,
      current: binding,
      bridge_status: "ready",
      event_sequence: 0,
      timeline: [],
      etag: '"thread-empty"',
    };
    const open: EditorialThreadRead = {
      kind: "open",
      thread_id: "thread-1",
      origin: binding,
      current: binding,
      binding_state: "valid",
      previous_sessions: [],
      bridge_status: "generating",
      event_sequence: 1,
      timeline: [
        {
          id: "entry-1",
          sequence: 1,
          kind: "user_message",
          actor_id: null,
          proposal_id: null,
          created_at: "2026-11-19T10:00:00Z",
          body: "Clarifier le contexte de ce passage",
        },
      ],
      etag: '"thread-generating"',
    };
    let threadReadCount = 0;
    const fetchMock = vi.fn<typeof fetch>((_input, init) => {
      if (init?.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              kind: "accepted",
              thread_id: "thread-1",
              entry_id: "entry-1",
              generation: "queued",
            }),
            { status: 202, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      threadReadCount += 1;
      return Promise.resolve(
        threadResponse(threadReadCount > 1 ? open : unopened),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const key = vi
      .spyOn(crypto, "randomUUID")
      .mockReturnValue("00000000-0000-4000-8000-000000000001");
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
          onChangeTarget={() => undefined}
        />
      </QueryClientProvider>,
    );

    const textarea = await screen.findByRole("textbox", {
      name: "Votre message à Bridge",
    });
    fireEvent.click(
      screen.getByRole("button", {
        name: "Clarifier le contexte de ce passage",
      }),
    );
    expect(textarea).toHaveValue("Clarifier le contexte de ce passage");
    expect(threadReadCount).toBe(1);

    fireEvent.change(textarea, { target: { value: "Instruction\ncomplétée" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const postCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    if (!postCall) throw new Error("message POST was not sent");
    const postInit = postCall[1];
    expect(new Headers(postInit?.headers).get("Idempotency-Key")).toBe(
      "00000000-0000-4000-8000-000000000001",
    );
    expect(postInit?.body).toBe(
      JSON.stringify({ kind: "message", text: "Instruction\ncomplétée" }),
    );
    expect(key).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Message envoyé.")).toBeVisible();
    const sendAnnouncement = screen.getByText("Message envoyé.");
    expect(sendAnnouncement).toHaveFocus();

    const queryKey = editorialWorkspaceQueryKeys.thread(
      binding.subject_id,
      binding.occurrence_id,
      "snapshot-1",
    );
    const replyAvailable: EditorialThreadRead = {
      ...open,
      bridge_status: "reply_available",
      event_sequence: 2,
      timeline: [
        ...open.timeline,
        {
          id: "reply-1",
          sequence: 2,
          kind: "bridge_reply",
          actor_id: null,
          proposal_id: null,
          created_at: "2026-11-19T10:00:02Z",
          body: "Réponse arrivée après l’envoi.",
        },
      ],
    };
    act(() => {
      client.setQueryData(queryKey, replyAvailable);
    });
    expect(
      await screen.findByText("Réponse arrivée après l’envoi."),
    ).toBeVisible();
    await waitFor(() => expect(textarea).toHaveFocus());
  });

  it("reuses one Idempotency-Key for transport retries", async () => {
    const key = vi
      .spyOn(crypto, "randomUUID")
      .mockReturnValue("00000000-0000-4000-8000-000000000002");
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockRejectedValueOnce(new TypeError("network lost"))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            kind: "accepted",
            thread_id: "thread-1",
            entry_id: "entry-1",
            generation: "queued",
          }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    await sendEditorialThreadMessage("subject-1", "occurrence-1", "Clarifier.");

    expect(key).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(
      fetchMock.mock.calls.map(([, init]) =>
        new Headers(init?.headers).get("Idempotency-Key"),
      ),
    ).toEqual([
      "00000000-0000-4000-8000-000000000002",
      "00000000-0000-4000-8000-000000000002",
    ]);
  });

  it("shows stable French errors and offers no retry after an uncertain send", async () => {
    const unopened: EditorialThreadRead = {
      kind: "unopened",
      binding,
      current: binding,
      bridge_status: "ready",
      event_sequence: 0,
      timeline: [],
      etag: '"thread-empty"',
    };
    const fetchMock = vi.fn<typeof fetch>((_input, init) => {
      if (init?.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              detail: {
                code: "reconciliation_required",
                message: "unsafe raw text",
              },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(threadResponse(unopened));
    });
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
          onChangeTarget={() => undefined}
        />
      </QueryClientProvider>,
    );
    fireEvent.change(
      await screen.findByRole("textbox", { name: "Votre message à Bridge" }),
      { target: { value: "Clarifier." } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Envoyer un message" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "L’état de votre envoi est incertain. Consultez le fil avant de continuer.",
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(
      screen.getByRole("textbox", { name: "Votre message à Bridge" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Envoyer un message" }),
    ).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: /réessayer/i }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(1);
  });

  it("reconciles an uncertain send and unlocks only after a persisted no-submit outcome", async () => {
    const uncertain: EditorialThreadRead = {
      kind: "open",
      thread_id: "thread-1",
      origin: binding,
      current: binding,
      binding_state: "valid",
      previous_sessions: [],
      bridge_status: "reconciliation_required",
      event_sequence: 2,
      timeline: [],
      etag: '"thread-uncertain"',
    };
    const ready: EditorialThreadRead = {
      ...uncertain,
      bridge_status: "ready",
      event_sequence: 3,
      etag: '"thread-not-submitted"',
    };
    let current = uncertain;
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (init?.method === "POST" && url.endsWith("/reconcile")) {
        current = ready;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              thread_id: "thread-1",
              outcome: "not_submitted",
              message:
                "Le message n’a pas été reçu… Vous pouvez en envoyer un nouveau.",
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(threadResponse(current));
    });
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
          onChangeTarget={() => undefined}
        />
      </QueryClientProvider>,
    );

    const composer = await screen.findByRole("textbox", {
      name: "Votre message à Bridge",
    });
    expect(composer).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "Générer une proposition" }),
    ).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Vérifier l’état de l’envoi" }),
    );

    expect(
      await screen.findByText(
        "Le message n’a pas été reçu… Vous pouvez en envoyer un nouveau.",
      ),
    ).toBeVisible();
    await waitFor(() => expect(composer).toBeEnabled());
    const reconcileCall = fetchMock.mock.calls.find(
      ([, init]) => String(init?.method).toUpperCase() === "POST",
    );
    expect(reconcileCall?.[0]).toContain("/reconcile");
    expect(
      new Headers(reconcileCall?.[1]?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
  });

  it("starts a new discussion after session loss and shows prior sessions read-only", async () => {
    const lost: EditorialThreadRead = {
      kind: "open",
      thread_id: "thread-1",
      origin: binding,
      current: binding,
      binding_state: "valid",
      previous_sessions: [],
      bridge_status: "session_lost",
      event_sequence: 1,
      timeline: [],
      etag: '"thread-lost"',
    };
    const currentSession: EditorialThreadRead = {
      ...lost,
      thread_id: "thread-2",
      bridge_status: "ready",
      previous_sessions: [1],
      event_sequence: 0,
      timeline: [],
      etag: '"thread-session-two"',
    };
    let current = lost;
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (init?.method === "POST" && url.endsWith("/entries")) {
        expect(init.body).toBe(
          JSON.stringify({ kind: "message", start_new_session: true }),
        );
        current = currentSession;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              kind: "session_started",
              thread_id: "thread-2",
              session_number: 2,
            }),
            { status: 202, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(threadResponse(current));
    });
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
          onChangeTarget={() => undefined}
        />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText(
        "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre",
      ),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Générer une proposition" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Démarrer une nouvelle discussion" }),
    ).toBeEnabled();
    expect(screen.queryByRole("button", { name: /réessayer/i })).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Démarrer une nouvelle discussion" }),
    );

    expect(await screen.findByText("Session précédente · 1")).toBeVisible();
    expect(
      await screen.findByRole("textbox", { name: "Votre message à Bridge" }),
    ).toBeEnabled();
    expect(screen.getByText("Nouvelle discussion démarrée.")).toBeVisible();
  });

  it("shows the French message for rejected idempotency keys", async () => {
    const lost: EditorialThreadRead = {
      kind: "open",
      thread_id: "thread-1",
      origin: binding,
      current: binding,
      binding_state: "valid",
      previous_sessions: [],
      bridge_status: "session_lost",
      event_sequence: 1,
      timeline: [],
      etag: '"thread-lost"',
    };
    const fetchMock = vi.fn<typeof fetch>((_input, init) => {
      if (init?.method === "POST") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              detail: {
                code: "idempotency_key_invalid",
                message: "raw backend detail",
              },
            }),
            { status: 422, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(threadResponse(lost));
    });
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
          onChangeTarget={() => undefined}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Démarrer une nouvelle discussion",
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La clé d’idempotence doit être un UUID valide.",
    );
  });

  it("replays a lost new-session response with the same key after refreshing the thread", async () => {
    const idempotencyKey = "00000000-0000-4000-8000-000000000003";
    const key = vi.spyOn(crypto, "randomUUID").mockReturnValue(idempotencyKey);
    const lost: EditorialThreadRead = {
      kind: "open",
      thread_id: "thread-1",
      origin: binding,
      current: binding,
      binding_state: "valid",
      previous_sessions: [],
      bridge_status: "session_lost",
      event_sequence: 1,
      timeline: [],
      etag: '"thread-lost"',
    };
    const currentSession: EditorialThreadRead = {
      ...lost,
      thread_id: "thread-2",
      bridge_status: "ready",
      previous_sessions: [1],
      event_sequence: 0,
      timeline: [],
      etag: '"thread-session-two"',
    };
    let threadReads = 0;
    let startRequests = 0;
    let committedKey: string | null = null;
    let releaseRefresh!: () => void;
    const refreshGate = new Promise<void>((resolve) => {
      releaseRefresh = resolve;
    });
    const requestKeys: string[] = [];
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      if (init?.method === "POST" && url.endsWith("/entries")) {
        startRequests += 1;
        const requestKey = new Headers(init.headers).get("Idempotency-Key");
        if (requestKey) requestKeys.push(requestKey);
        expect(init.body).toBe(
          JSON.stringify({ kind: "message", start_new_session: true }),
        );
        if (startRequests === 1) {
          committedKey = requestKey;
          return Promise.reject(new TypeError("response lost after commit"));
        }
        expect(committedKey).toBe(idempotencyKey);
        expect(requestKey).toBe(committedKey);
        return Promise.resolve(
          new Response(
            JSON.stringify({
              kind: "session_started",
              thread_id: "thread-2",
              session_number: 2,
            }),
            { status: 202, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      threadReads += 1;
      if (threadReads === 2) {
        return refreshGate.then(() => threadResponse(lost));
      }
      return Promise.resolve(
        threadResponse(startRequests > 1 ? currentSession : lost),
      );
    });
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
          onChangeTarget={() => undefined}
        />
      </QueryClientProvider>,
    );

    const startButton = await screen.findByRole("button", {
      name: "Démarrer une nouvelle discussion",
    });
    fireEvent.click(startButton);

    await waitFor(() => expect(threadReads).toBe(2));
    expect(startButton).toBeDisabled();
    expect(committedKey).toBe(idempotencyKey);
    releaseRefresh();

    await waitFor(() => expect(startButton).toBeEnabled());
    fireEvent.click(startButton);

    expect(
      await screen.findByText("Nouvelle discussion démarrée."),
    ).toBeVisible();
    expect(requestKeys).toEqual([idempotencyKey, idempotencyKey]);
    expect(key).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Session précédente · 1")).toBeVisible();
  });
});
