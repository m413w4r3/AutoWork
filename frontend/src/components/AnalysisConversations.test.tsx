import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnalysisConversations } from "./AnalysisConversations";

const subjectId = "30e5b0b8-2dba-48c3-81ca-9eaed5c22c62";
const conversationId = "879635e2-32d1-4b48-ac4a-ac56f919003d";

afterEach(() => vi.unstubAllGlobals());

describe("AnalysisConversations", () => {
  it("crée, sélectionne, continue et archive sans présenter les sorties comme preuves", async () => {
    let conversation: Record<string, unknown> | null = null;
    let turnCount = 0;
    const requests: Array<{ url: string; body?: Record<string, unknown> }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        await Promise.resolve();
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.toString()
              : input.url;
        const body =
          typeof init?.body === "string"
            ? (JSON.parse(init.body) as Record<string, unknown>)
            : undefined;
        requests.push({ url, body });
        if (url.includes("/turns")) {
          if (init?.method === "POST") {
            turnCount += 1;
            conversation = {
              ...conversation!,
              turn_count: turnCount,
              status: "ready",
            };
            return Response.json({
              id: `turn-${turnCount}`,
              sequence: turnCount,
              model_run_id: `run-${turnCount}`,
              correlation_id: `corr-${turnCount}`,
              status: "succeeded",
              input_text: null,
              output_text: null,
              error: null,
            });
          }
          return Response.json(
            Array.from({ length: turnCount }, (_, index) => ({
              id: `turn-${index + 1}`,
              sequence: index + 1,
              model_run_id: `run-${index + 1}`,
              correlation_id: `corr-${index + 1}`,
              status: "succeeded",
              input_text: `Question ${index + 1}`,
              output_text: `Réponse ${index + 1}`,
              error: null,
            })),
          );
        }
        if (url.endsWith("/archive?subject_id=" + subjectId)) {
          conversation = { ...conversation!, status: "archived" };
          return Response.json(conversation);
        }
        if (init?.method === "POST") {
          conversation = {
            id: conversationId,
            provider: "openai",
            transport: "chatgpt_bridge",
            purpose: "analyst_assistance",
            subject_id: subjectId,
            title: "Analyse A",
            status: "pending",
            requested_model: null,
            expected_profile: null,
            turn_count: 0,
            last_used_at: null,
            evidence_warning: "not_primary_evidence",
          };
          return Response.json(conversation, { status: 201 });
        }
        return Response.json(conversation ? [conversation] : []);
      }),
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={client}>
        <AnalysisConversations subjectId={subjectId} />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText("Aucune conversation pour ce sujet."),
    ).toBeVisible();
    expect(screen.getByText(/ne sont ni des preuves primaires/)).toBeVisible();
    await user.type(
      screen.getByLabelText("Titre défini par l’application"),
      "Analyse A",
    );
    await user.click(
      screen.getByRole("button", { name: "Nouvelle conversation" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Analyse A" }),
    ).toBeVisible();

    await user.type(screen.getByLabelText("Question"), "Question 1");
    await user.click(
      screen.getByLabelText(/La classification et la politique de diffusion/),
    );
    await user.click(
      screen.getByRole("button", { name: "Envoyer le premier message" }),
    );
    expect(await screen.findByText("Réponse 1")).toBeVisible();
    await user.type(screen.getByLabelText("Question"), "Question 2");
    await user.click(
      screen.getByRole("button", { name: "Continuer cette conversation" }),
    );
    expect(await screen.findByText("Réponse 2")).toBeVisible();

    const postedTurns = requests.filter(
      (request) => request.url.includes("/turns") && request.body,
    );
    expect(postedTurns.map((request) => request.body?.mode)).toEqual([
      "fresh",
      "continue",
    ]);
    await user.click(screen.getByRole("button", { name: "Archiver" }));
    await waitFor(() =>
      expect(screen.queryByLabelText("Question")).not.toBeInTheDocument(),
    );
  });
});
