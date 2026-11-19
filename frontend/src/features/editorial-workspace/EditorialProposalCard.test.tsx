import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
    expect(screen.getByText("Proposition à consulter")).toBeVisible();
    expect(screen.getByText("Base canonique · version 4")).toBeVisible();
    expect(screen.getByRole("button", { name: "Rejeter" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Accepter" })).toBeNull();
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
});
