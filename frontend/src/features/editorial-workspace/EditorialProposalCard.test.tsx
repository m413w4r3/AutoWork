import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";
import { EditorialProposalCard } from "./EditorialProposalCard";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("EditorialProposalCard", () => {
  it("loads the persisted proposal as read-only content", async () => {
    const proposal = {
      id: "proposal-1",
      subject_id: "subject-1",
      production_run_id: "run-1",
      occurrence_id: "occurrence-1",
      thread_id: "thread-1",
      origin: "manual_patch",
      state: "proposed",
      state_version: 1,
      operation: "rewrite_title",
      base_artifact_id: "artifact-1",
      base_artifact_version: 4,
      base_canonical_sha256: "a".repeat(64),
      base_text: "Titre actuel synthétique",
      patch_sha256: "b".repeat(64),
      patch: {
        schema_version: "editorial-patch-v1",
        operation: "rewrite_title",
        target: { kind: "title" },
        target_id: "target-1",
        occurrence_id: "occurrence-1",
        before_hash: "c".repeat(64),
        after: "Titre proposé synthétique",
        rationale: "Raison synthétique",
      },
      diagnostics: {},
      created_at: "2026-11-19T12:00:00Z",
    };
    const fetchMock = vi.fn<typeof fetch>(() =>
      Promise.resolve(
        new Response(JSON.stringify(proposal), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <EditorialProposalCard
          subjectId="subject-1"
          occurrenceId="occurrence-1"
          proposalId="proposal-1"
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Titre proposé synthétique")).toBeVisible();
    expect(screen.getByText("Modification manuelle")).toBeVisible();
    expect(screen.getByText("Proposition à consulter")).toBeVisible();
    expect(screen.getByText("Base canonique · version 4")).toBeVisible();
    expect(screen.getByRole("button", { name: "Accepter" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Rejeter" })).toBeVisible();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/subjects/subject-1/editor/threads/occurrence-1/proposals/proposal-1",
      undefined,
    );
  });

  it("makes a structured enrichment patch inspectable in the details", async () => {
    const proposal = {
      id: "proposal-2",
      subject_id: "subject-1",
      production_run_id: "run-1",
      occurrence_id: "occurrence-1",
      thread_id: "thread-1",
      state: "proposed",
      state_version: 1,
      operation: "rewrite_paragraph",
      base_artifact_id: "artifact-1",
      base_artifact_version: 4,
      base_canonical_sha256: "a".repeat(64),
      base_text: "Texte actuel synthétique",
      patch_sha256: "b".repeat(64),
      patch: {
        schema_version: "editorial-patch-v1",
        operation: "rewrite_paragraph",
        target: { kind: "paragraph" },
        target_id: "target-1",
        occurrence_id: "occurrence-1",
        before_hash: "c".repeat(64),
        enrichment_patch: { text: "Paragraphe proposé synthétique" },
        rationale: "Raison synthétique",
      },
      diagnostics: {},
      created_at: "2026-11-19T12:00:00Z",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() =>
        Promise.resolve(
          new Response(JSON.stringify(proposal), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <EditorialProposalCard
          subjectId="subject-1"
          occurrenceId="occurrence-1"
          proposalId="proposal-2"
        />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText("Patch éditorial typé disponible."),
    ).toBeVisible();
    fireEvent.click(screen.getByText("Détails de la proposition"));
    expect(screen.getByLabelText("Patch éditorial typé")).toHaveTextContent(
      "Paragraphe proposé synthétique",
    );
  });

  it("shows a bounded French reason for a Bridge refusal", async () => {
    const proposal = {
      id: "proposal-refused",
      subject_id: "subject-1",
      production_run_id: "run-1",
      occurrence_id: "occurrence-1",
      thread_id: "thread-1",
      state: "refused",
      state_version: 1,
      operation: "rewrite_title",
      base_artifact_id: "artifact-1",
      base_artifact_version: 4,
      base_canonical_sha256: "a".repeat(64),
      base_text: null,
      patch_sha256: null,
      patch: null,
      diagnostics: { code: "revision_not_applicable" },
      created_at: "2026-11-19T12:00:00Z",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() =>
        Promise.resolve(
          new Response(JSON.stringify(proposal), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <EditorialProposalCard
          subjectId="subject-1"
          occurrenceId="occurrence-1"
          proposalId="proposal-refused"
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Refus Bridge")).toBeVisible();
    expect(
      screen.getByText(
        "Les données disponibles ne permettent pas de proposer cette révision.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByText(/données synthétiques insuffisantes/i),
    ).toBeNull();
  });

  it("rejects a proposal with its state version and patch hash", async () => {
    const proposal = {
      id: "proposal-reject",
      subject_id: "subject-1",
      production_run_id: "run-1",
      occurrence_id: "occurrence-1",
      thread_id: "thread-1",
      state: "proposed",
      state_version: 3,
      operation: "rewrite_paragraph",
      base_artifact_id: "artifact-1",
      base_artifact_version: 4,
      base_canonical_sha256: "a".repeat(64),
      base_text: "Paragraphe actuel",
      patch_sha256: "b".repeat(64),
      patch: {
        schema_version: "editorial-patch-v1",
        operation: "rewrite_paragraph",
        target: { kind: "paragraph" },
        target_id: "target-1",
        occurrence_id: "occurrence-1",
        before_hash: "c".repeat(64),
        after: "Paragraphe proposé",
        rationale: "Raison synthétique",
      },
      diagnostics: {},
      created_at: "2026-11-19T12:00:00Z",
    };
    let currentState = "proposed";
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url = input instanceof Request ? input.url : String(input);
      if (init?.method === "POST") {
        const idempotencyKey = new Headers(init.headers).get("Idempotency-Key");
        expect(idempotencyKey).toMatch(/^[A-Za-z0-9_-]{16,128}$/);
        expect(init.body).toBe(
          JSON.stringify({
            expected_state_version: 3,
            patch_sha256: "b".repeat(64),
          }),
        );
        expect(url).toContain("/proposals/proposal-reject/reject");
        currentState = "rejected";
        return Promise.resolve(
          new Response(
            JSON.stringify({
              id: "proposal-reject",
              state: "rejected",
              state_version: 4,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ ...proposal, state: currentState }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <EditorialProposalCard
          subjectId="subject-1"
          occurrenceId="occurrence-1"
          proposalId="proposal-reject"
        />
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Rejeter" }));

    expect(await screen.findByText("Rejetée")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Rejeter" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Accepter" })).toBeNull();
  });

  it("accepts through the API layer, keeps focus, and invalidates workspace, thread, and sibling proposals", async () => {
    const proposal = {
      id: "proposal-accept",
      subject_id: "subject-1",
      production_run_id: "run-1",
      occurrence_id: "occurrence-1",
      thread_id: "thread-1",
      state: "proposed",
      state_version: 3,
      operation: "rewrite_paragraph",
      base_artifact_id: "artifact-1",
      base_artifact_version: 4,
      base_canonical_sha256: "a".repeat(64),
      base_text: "Paragraphe actuel",
      patch_sha256: "b".repeat(64),
      patch: {
        schema_version: "editorial-patch-v1",
        operation: "rewrite_paragraph",
        target: { kind: "paragraph" },
        target_id: "target-1",
        occurrence_id: "occurrence-1",
        before_hash: "c".repeat(64),
        after: "Paragraphe proposé",
        rationale: "Raison synthétique",
      },
      diagnostics: {},
      created_at: "2026-11-19T12:00:00Z",
    };
    let currentState = "proposed";
    let decisionCalls = 0;
    let resolveAcceptance: (response: Response) => void = () => undefined;
    const pendingAcceptance = new Promise<Response>((resolve) => {
      resolveAcceptance = resolve;
    });
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url = input instanceof Request ? input.url : String(input);
      if (init?.method === "POST") {
        decisionCalls += 1;
        expect(url).toContain("/proposals/proposal-accept/accept");
        expect(new Headers(init.headers).get("Idempotency-Key")).toMatch(
          /^[A-Za-z0-9_-]{16,128}$/,
        );
        expect(init.body).toBe(
          JSON.stringify({
            expected_state_version: 3,
            patch_sha256: "b".repeat(64),
          }),
        );
        currentState = "accepted";
        return pendingAcceptance;
      }
      return Promise.resolve(
        new Response(JSON.stringify({ ...proposal, state: currentState }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const invalidate = vi.spyOn(client, "invalidateQueries");

    render(
      <QueryClientProvider client={client}>
        <EditorialProposalCard
          subjectId="subject-1"
          occurrenceId="occurrence-1"
          proposalId="proposal-accept"
        />
      </QueryClientProvider>,
    );

    const acceptButton = await screen.findByRole("button", {
      name: "Accepter",
    });
    acceptButton.focus();
    fireEvent.click(acceptButton);

    expect(
      await screen.findByRole("button", { name: "Acceptation en cours…" }),
    ).toBeVisible();
    const rejectButton = screen.getByRole("button", { name: "Rejeter" });
    expect(rejectButton).toBeDisabled();
    fireEvent.click(rejectButton);
    expect(decisionCalls).toBe(1);
    resolveAcceptance(
      new Response(
        JSON.stringify({
          id: "proposal-accept",
          state: "accepted",
          state_version: 4,
          artifact_id: "artifact-2",
          artifact_version: 5,
          snapshot_id: "snapshot-2",
          impact_id: "impact-1",
          impact_state: "pending",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    expect(
      await screen.findByRole("button", { name: "Acceptée" }),
    ).toBeVisible();
    expect(document.activeElement).toBe(acceptButton);
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: editorialWorkspaceQueryKeys.snapshot("subject-1"),
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: editorialWorkspaceQueryKeys.threadsForOccurrence(
        "subject-1",
        "occurrence-1",
      ),
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: editorialWorkspaceQueryKeys.proposalsForOccurrence(
        "subject-1",
        "occurrence-1",
      ),
    });
  });

  it("locks acceptance while rejection is pending", async () => {
    const proposal = {
      id: "proposal-reject-pending",
      subject_id: "subject-1",
      production_run_id: "run-1",
      occurrence_id: "occurrence-1",
      thread_id: "thread-1",
      state: "proposed",
      state_version: 1,
      operation: "rewrite_paragraph",
      base_artifact_id: "artifact-1",
      base_artifact_version: 4,
      base_canonical_sha256: "a".repeat(64),
      base_text: "Paragraphe actuel",
      patch_sha256: "b".repeat(64),
      patch: {
        schema_version: "editorial-patch-v1",
        operation: "rewrite_paragraph",
        target: { kind: "paragraph" },
        target_id: "target-1",
        occurrence_id: "occurrence-1",
        before_hash: "c".repeat(64),
        after: "Paragraphe proposé",
        rationale: "Raison synthétique",
      },
      diagnostics: {},
      created_at: "2026-11-19T12:00:00Z",
    };
    let acceptCalls = 0;
    let currentState = "proposed";
    let resolveRejection: (response: Response) => void = () => undefined;
    const pendingRejection = new Promise<Response>((resolve) => {
      resolveRejection = resolve;
    });
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url = input instanceof Request ? input.url : String(input);
      if (init?.method === "POST" && url.endsWith("/reject")) {
        return pendingRejection;
      }
      if (init?.method === "POST") {
        acceptCalls += 1;
        return Promise.resolve(
          new Response(
            JSON.stringify({
              detail: { code: "proposal_state_conflict", message: "Conflit." },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify({ ...proposal, state: currentState }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <EditorialProposalCard
          subjectId="subject-1"
          occurrenceId="occurrence-1"
          proposalId="proposal-reject-pending"
        />
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Rejeter" }));

    expect(await screen.findByText("Rejet en cours…")).toBeVisible();
    const acceptButton = screen.getByRole("button", { name: "Accepter" });
    expect(acceptButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "Rejeter" })).toBeDisabled();
    fireEvent.click(acceptButton);
    expect(acceptCalls).toBe(0);

    currentState = "rejected";
    resolveRejection(
      new Response(
        JSON.stringify({
          id: proposal.id,
          state: "rejected",
          state_version: 2,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    expect(await screen.findByText("Rejetée")).toBeVisible();
    expect(acceptCalls).toBe(0);
  });

  it("shows a safe French conflict recovery path after a stale acceptance", async () => {
    const proposal = {
      id: "proposal-stale",
      subject_id: "subject-1",
      production_run_id: "run-1",
      occurrence_id: "occurrence-1",
      thread_id: "thread-1",
      state: "proposed",
      state_version: 1,
      operation: "rewrite_paragraph",
      base_artifact_id: "artifact-1",
      base_artifact_version: 4,
      base_canonical_sha256: "a".repeat(64),
      base_text: "Paragraphe actuel",
      patch_sha256: "b".repeat(64),
      patch: {
        schema_version: "editorial-patch-v1",
        operation: "rewrite_paragraph",
        target: { kind: "paragraph" },
        target_id: "target-1",
        occurrence_id: "occurrence-1",
        before_hash: "c".repeat(64),
        after: "Paragraphe proposé",
        rationale: "Raison synthétique",
      },
      diagnostics: {},
      created_at: "2026-11-19T12:00:00Z",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>((_input, init) =>
        Promise.resolve(
          init?.method === "POST"
            ? new Response(
                JSON.stringify({
                  detail: {
                    code: "proposal_base_stale",
                    message:
                      "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition.",
                  },
                }),
                {
                  status: 409,
                  headers: { "Content-Type": "application/json" },
                },
              )
            : new Response(JSON.stringify(proposal), {
                status: 200,
                headers: { "Content-Type": "application/json" },
              }),
        ),
      ),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const requestVariant = vi.fn();

    render(
      <QueryClientProvider client={client}>
        <EditorialProposalCard
          subjectId="subject-1"
          occurrenceId="occurrence-1"
          proposalId="proposal-stale"
          onRequestVariant={requestVariant}
        />
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Accepter" }));

    expect(
      await screen.findByText(
        "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition.",
      ),
    ).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Demander une variante" }),
    );
    expect(requestVariant).toHaveBeenCalledOnce();
    expect(
      screen.getByRole("button", { name: "Comparer à nouveau" }),
    ).toBeVisible();
  });
});
