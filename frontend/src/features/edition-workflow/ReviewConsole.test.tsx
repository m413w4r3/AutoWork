import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EditionReview, ReviewItem } from "../../api/publication";
import { ReviewConsole } from "./ReviewConsole";
import { reviewPollingInterval } from "./reviewPolling";

const EDITION_ID = "edition-1";
const HASH = "a".repeat(64);

interface FetchMock {
  mock: {
    calls: Array<[RequestInfo | URL, RequestInit?]>;
  };
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function bodyOf(init: RequestInit): string {
  if (typeof init.body !== "string") throw new Error("Expected a JSON body");
  return init.body;
}

const baseItem: ReviewItem = {
  position: 1,
  subject_id: "subject-1",
  title: "Article de test",
  run_id: "run-1",
  pipeline_generation: 3,
  run_status: "ready",
  document_artifact_id: "artifact-1",
  document_artifact_version: 2,
  document_input_hash: HASH,
  effective_decision_id: null,
  effective_decision: "include",
  included: true,
  blocking: false,
  can_retry: false,
  retry_stage: null,
  error_code: null,
  error_message: null,
};

function makeItem(overrides: Partial<ReviewItem> = {}): ReviewItem {
  return { ...baseItem, ...overrides };
}

function makeReview(
  items: ReviewItem[] = [baseItem],
  can_accept = true,
): EditionReview {
  return { edition_id: EDITION_ID, items, can_accept };
}

function decisionResponse() {
  return Response.json({
    id: "decision-1",
    edition_id: EDITION_ID,
    subject_id: "subject-1",
    production_run_id: "run-1",
    pipeline_generation: 3,
    document_artifact_id: "artifact-1",
    document_artifact_version: 2,
    document_input_hash: HASH,
    decision: "exclude",
    actor_id: "analyst-1",
    reason: "Hors périmètre",
    occurred_at: "2026-08-29T10:00:00Z",
  });
}

function renderReview(
  review: EditionReview,
  postResponse: () => Response = decisionResponse,
) {
  const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
    init?.method === "POST"
      ? Promise.resolve(postResponse())
      : Promise.resolve(Response.json(review)),
  );
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ReviewConsole editionId={EDITION_ID} />
    </QueryClientProvider>,
  );
  return { client, fetchMock };
}

function postCall(fetchMock: FetchMock) {
  const call = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
  if (!call || !call[1]) throw new Error("Expected a POST request");
  return [call[0], call[1]] as const;
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("ReviewConsole", () => {
  it("affiche un READY inclus par défaut et conserve la navigation interne", async () => {
    renderReview(makeReview([makeItem({ effective_decision: null })]));

    expect(await screen.findByText("Article de test")).toBeInTheDocument();
    expect(screen.getByText("Prêt")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Exclure" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Réinclure" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Ouvrir" })).toHaveAttribute(
      "href",
      "/subjects/subject-1",
    );
  });

  it("affiche Réinclure pour un READY exclu avec document", async () => {
    renderReview(
      makeReview([
        makeItem({ effective_decision: "exclude", included: false }),
      ]),
    );

    expect(await screen.findByText("Exclu")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Réinclure" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Exclure" }),
    ).not.toBeInTheDocument();
  });

  it.each(["failed", "needs_review"] as const)(
    "%s sans document peut être exclu",
    async (run_status) => {
      renderReview(
        makeReview([
          makeItem({
            run_status,
            effective_decision: null,
            included: false,
            blocking: true,
            can_retry: true,
            retry_stage: "synthesis",
            document_artifact_id: null,
            document_artifact_version: null,
            document_input_hash: null,
            error_message: "Intervention requise.",
          }),
        ]),
      );

      const card = await screen.findByText("Article de test");
      const itemCard = card.closest("li");
      expect(itemCard).not.toBeNull();
      expect(
        within(itemCard as HTMLElement).getByText("À corriger"),
      ).toBeInTheDocument();
      expect(
        within(itemCard as HTMLElement).getByRole("button", {
          name: "Exclure",
        }),
      ).toBeInTheDocument();
      expect(
        within(itemCard as HTMLElement).queryByRole("button", {
          name: "Réinclure",
        }),
      ).not.toBeInTheDocument();
    },
  );

  it("refuse une raison vide puis exclut avec les champs GET exacts, y compris null", async () => {
    const item = makeItem({
      run_id: "run-failed",
      pipeline_generation: 9,
      run_status: "failed",
      effective_decision: null,
      included: false,
      blocking: true,
      can_retry: true,
      retry_stage: "synthesis",
      document_artifact_id: null,
      document_artifact_version: null,
      document_input_hash: null,
    });
    const { fetchMock } = renderReview(makeReview([item]));
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Exclure" }));
    const confirm = screen.getByRole("button", { name: "Confirmer" });
    expect(confirm).toBeDisabled();
    await user.type(
      screen.getByLabelText("Raison de l’exclusion"),
      "  Hors périmètre  ",
    );
    await user.click(confirm);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const [, init] = postCall(fetchMock);
    expect(JSON.parse(bodyOf(init))).toEqual({
      production_run_id: "run-failed",
      pipeline_generation: 9,
      document_artifact_id: null,
      document_artifact_version: null,
      document_input_hash: null,
      reason: "Hors périmètre",
    });
  });

  it("réinclut avec l’identité exacte du document du GET", async () => {
    const item = makeItem({
      effective_decision: "exclude",
      included: false,
      run_id: "run-ready",
      pipeline_generation: 11,
      document_artifact_id: "artifact-ready",
      document_artifact_version: 7,
      document_input_hash: "b".repeat(64),
    });
    const { fetchMock } = renderReview(makeReview([item]));
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Réinclure" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const [, init] = postCall(fetchMock);
    expect(
      fetchMock.mock.calls.find(
        ([, callInit]) => callInit?.method === "POST",
      )?.[0],
    ).toBe("/api/editions/edition-1/review/items/subject-1/include");
    expect(JSON.parse(bodyOf(init))).toEqual({
      production_run_id: "run-ready",
      pipeline_generation: 11,
      document_artifact_id: "artifact-ready",
      document_artifact_version: 7,
      document_input_hash: "b".repeat(64),
    });
  });

  it("affiche le stale 409 et recharge immédiatement la revue", async () => {
    const { client, fetchMock } = renderReview(
      makeReview([
        makeItem({
          run_status: "failed",
          included: false,
          blocking: true,
          can_retry: true,
          retry_stage: "synthesis",
          document_artifact_id: null,
          document_artifact_version: null,
          document_input_hash: null,
        }),
      ]),
      () =>
        Response.json(
          { detail: { code: "review_item_stale" } },
          { status: 409 },
        ),
    );
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Exclure" }));
    await user.type(
      screen.getByLabelText("Raison de l’exclusion"),
      "Contexte modifié",
    );
    await user.click(screen.getByRole("button", { name: "Confirmer" }));

    expect(
      await screen.findByText(
        "Cet article a changé depuis son ouverture. La revue a été rechargée.",
      ),
    ).toBeInTheDocument();
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["edition-review", EDITION_ID],
    });
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method !== "POST"),
    ).toBe(true);
  });

  it("utilise run_id et retry_stage tels que retournés puis invalide les vues", async () => {
    const item = makeItem({
      run_id: "run-retry",
      run_status: "failed",
      effective_decision: null,
      included: false,
      blocking: true,
      can_retry: true,
      retry_stage: "synthesis",
      document_artifact_id: null,
      document_artifact_version: null,
      document_input_hash: null,
    });
    const { client, fetchMock } = renderReview(makeReview([item]));
    client.setQueryData(["subject-production", item.subject_id], {
      status: "failed",
    });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Réessayer" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const [url, init] = postCall(fetchMock);
    expect(url).toBe("/api/production/runs/run-retry/retry");
    expect(JSON.parse(bodyOf(init))).toEqual({ stage: "synthesis" });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["edition-review", EDITION_ID],
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["batch", EDITION_ID],
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["edition", EDITION_ID],
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["subject-production", item.subject_id],
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["subject-content", item.subject_id],
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["subject-indicators", item.subject_id],
    });
  });

  it("ne dérive pas retry_stage depuis un item non retryable", async () => {
    renderReview(
      makeReview([
        makeItem({
          run_status: "failed",
          can_retry: false,
          retry_stage: "synthesis",
          included: false,
          blocking: true,
        }),
      ]),
    );

    await screen.findByText("Article de test");
    expect(
      screen.queryByRole("button", { name: "Réessayer" }),
    ).not.toBeInTheDocument();
  });

  it("polling seulement quand un item est queued ou running", () => {
    expect(
      reviewPollingInterval(makeReview([makeItem({ run_status: "queued" })])),
    ).toBe(2_000);
    expect(
      reviewPollingInterval(makeReview([makeItem({ run_status: "running" })])),
    ).toBe(2_000);
    expect(reviewPollingInterval(makeReview([baseItem]))).toBe(false);
    expect(reviewPollingInterval(undefined)).toBe(false);
  });

  it("respecte included, blocking, l’ordre GET et désactive Accept", async () => {
    const items = [
      makeItem({
        position: 2,
        subject_id: "subject-2",
        title: "Deuxième",
        effective_decision: null,
        included: false,
        blocking: false,
      }),
      makeItem({
        position: 1,
        subject_id: "subject-1",
        title: "Premier",
        included: true,
        blocking: true,
      }),
    ];
    renderReview(makeReview(items, false));

    expect(await screen.findByText("Deuxième")).toBeInTheDocument();
    const cards = screen.getAllByRole("listitem");
    expect(cards[0]).toHaveTextContent("Deuxième");
    expect(cards[1]).toHaveTextContent("Premier");
    expect(screen.getByText("1 inclus")).toBeInTheDocument();
    expect(screen.getByText("1 à corriger")).toBeInTheDocument();
    expect(screen.getByText("0 exclus")).toBeInTheDocument();
    expect(
      screen.getByText("Résolvez ou excluez les articles bloquants."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Accepter la production" }),
    ).toBeDisabled();
  });

  it("active Accept sans body métier et invalide la publication et l’édition", async () => {
    const { client, fetchMock } = renderReview(makeReview());
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Accepter la production" }),
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
      ).toBe(true),
    );
    const [url, init] = postCall(fetchMock);
    expect(url).toBe("/api/editions/edition-1/publication/accept");
    expect(init.body).toBeUndefined();
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["edition-release", EDITION_ID],
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["edition", EDITION_ID],
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["editions"] });
  });

  it("recharge la revue quand le backend refuse l’acceptation", async () => {
    const { client } = renderReview(makeReview(), () =>
      Response.json(
        { detail: { code: "review_cannot_be_accepted" } },
        { status: 409 },
      ),
    );
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Accepter la production" }),
    );

    expect(
      await screen.findByText(
        "La revue ne peut plus être acceptée. Rechargez la revue.",
      ),
    ).toBeInTheDocument();
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["edition-review", EDITION_ID],
    });
  });

  it("ne demande aucun artifact lourd depuis la revue", async () => {
    const { fetchMock } = renderReview(makeReview());
    await screen.findByText("Article de test");
    expect(
      fetchMock.mock.calls.every(
        ([input]) => !urlOf(input).includes("artifacts"),
      ),
    ).toBe(true);
  });
});
