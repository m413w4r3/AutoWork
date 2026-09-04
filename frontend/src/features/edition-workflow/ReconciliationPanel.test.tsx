import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi, type Mock } from "vitest";

import { ReconciliationPanel } from "./ReconciliationPanel";

const reconciliation = {
  production_run_id: "run-1",
  model_run_id: "model-1",
  bridge_response_id: "bridge-1",
  submission_state: "submitted_or_unknown",
  phase: "reconciliation",
  stage: "extraction" as const,
  pipeline_generation: 2,
  output_sha256: null,
  provenance: null,
  visible_available: false,
  batch_id: null,
};

type FetchFunction = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;
type FetchMock = Mock<FetchFunction>;

function renderPanel(fetchMock: FetchMock) {
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const onRecovered = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <ReconciliationPanel
        runId="run-1"
        reconciliation={reconciliation}
        onRecovered={onRecovered}
      />
    </QueryClientProvider>,
  );
  return onRecovered;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ReconciliationPanel", () => {
  it("garde la déclaration perdue repliée et affiche le résultat de la sonde", async () => {
    const fetchMock = vi.fn<FetchFunction>(() =>
      Promise.resolve(
        Response.json({ outcome: "undecided", bridge_status: "running" }),
      ),
    );
    renderPanel(fetchMock);

    const summary = screen.getByText(
      "La réponse ChatGPT est définitivement perdue",
    );
    const details = summary.closest("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(
      screen.getByRole("button", { name: "Déclarer perdue et débloquer" }),
    ).not.toBeVisible();

    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Vérifier auprès du bridge" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Le bridge n’a pas pu trancher. Réessayez plus tard, ou déclarez la réponse perdue.",
    );
  });

  it("déclare la réponse perdue avec la raison saisie et recharge le panneau", async () => {
    const fetchMock = vi.fn<FetchFunction>((input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url;
      return Promise.resolve(
        Response.json(
          url.endsWith("/declare-lost")
            ? { outcome: "released", declared_lost: true }
            : { outcome: "undecided", bridge_status: null },
        ),
      );
    });
    const onRecovered = renderPanel(fetchMock);
    const user = userEvent.setup();

    await user.click(
      screen.getByText("La réponse ChatGPT est définitivement perdue"),
    );
    expect(
      screen.getByRole("button", { name: "Déclarer perdue et débloquer" }),
    ).toHaveClass("button--danger");
    expect(
      screen.getByText(
        "Cette action autorise une nouvelle soumission du même prompt. Si le modèle avait déjà répondu, cette réponse sera perdue et le coût sera payé deux fois. Ne l’utilisez qu’après avoir vérifié auprès du bridge et cherché la conversation dans l’historique ChatGPT.",
      ),
    ).toBeInTheDocument();

    await user.type(
      screen.getByLabelText("Raison (facultatif)"),
      "Historique vérifié",
    );
    await user.click(
      screen.getByRole("button", { name: "Déclarer perdue et débloquer" }),
    );

    expect(onRecovered).toHaveBeenCalledTimes(1);
    const posts = fetchMock.mock.calls.filter(
      ([, init]) => init?.method === "POST",
    );
    expect(posts).toHaveLength(1);
    expect(posts[0]?.[0]).toBe(
      "/api/production/runs/run-1/reconciliation/declare-lost",
    );
    expect(JSON.parse(posts[0]?.[1]?.body as string)).toEqual({
      confirm: true,
      reason: "Historique vérifié",
    });
  });
});
