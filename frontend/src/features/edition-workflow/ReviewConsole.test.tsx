import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditionRepairPage,
  EditionReview,
  ReviewItem,
} from "../../api/publication";
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
  rejected_indicator_count: 0,
  rejected_rule_count: 0,
  published_rule_count: 0,
  can_retry: false,
  retry_stage: null,
  requires_reconciliation: false,
  reconciliation: null,
  error_code: null,
  error_message: null,
};

function makeItem(overrides: Partial<ReviewItem> = {}): ReviewItem {
  return { ...baseItem, ...overrides };
}

function reconciliationItem(): ReviewItem {
  return makeItem({
    run_id: "run-reconcile",
    run_status: "needs_review",
    effective_decision: null,
    included: false,
    blocking: true,
    can_retry: false,
    retry_stage: null,
    requires_reconciliation: true,
    reconciliation: {
      production_run_id: "run-reconcile",
      model_run_id: "model-run-1",
      bridge_response_id: "bridge-1",
      submission_state: "submitted_or_unknown",
      phase: "reconciliation",
      stage: "synthesis",
      pipeline_generation: 3,
      output_sha256: null,
      provenance: null,
      visible_available: true,
      batch_id: null,
    },
    document_artifact_id: null,
    document_artifact_version: null,
    document_input_hash: null,
    error_code: "model_submission_reconciliation_required",
    error_message: "Soumission ambiguë.",
  });
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

const emptyRepairPage: EditionRepairPage = {
  summary: {
    unresolved_total: 0,
    sources_to_supply: 0,
    rejected_iocs_to_review: 0,
    rejected_rules_to_review: 0,
    rejected_other_artifacts: 0,
    articles_with_repairs: 0,
    articles_needing_rebuild: 0,
  },
  items: [],
  articles: [],
  next_cursor: null,
};

function renderReview(
  review: EditionReview,
  postResponse: () => Response | Promise<Response> = decisionResponse,
) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") return Promise.resolve(postResponse());
    return Promise.resolve(
      Response.json(
        urlOf(input).includes("/review/repairs") ? emptyRepairPage : review,
      ),
    );
  });
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

function postCalls(fetchMock: FetchMock) {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method === "POST")
    .map(([input, init]) => [urlOf(input), init as RequestInit] as const);
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

    await waitFor(() => expect(postCalls(fetchMock)).toHaveLength(1));
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
    await waitFor(() => expect(postCalls(fetchMock)).toHaveLength(1));
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
    await waitFor(() => expect(postCalls(fetchMock)).toHaveLength(1));
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

  it("arrête exclusivement le run_id affiché et invalide les vues liées", async () => {
    const item = makeItem({
      subject_id: "subject-stale-card",
      run_id: "run-old",
      run_status: "running",
    });
    const { client, fetchMock } = renderReview(makeReview([item]));
    client.setQueryData(["subject-production", item.subject_id], {
      run_id: "run-new",
      status: "running",
    });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", {
        name: "Arrêter cette tentative",
      }),
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
      ).toHaveLength(1),
    );
    const [url] = postCall(fetchMock);
    expect(url).toBe("/api/production/runs/run-old/cancel");
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

  it("affiche Arrêt… pendant l’annulation et rend l’erreur sans retry", async () => {
    let resolvePost: (response: Response) => void = () => undefined;
    const pendingPost = new Promise<Response>((resolve) => {
      resolvePost = resolve;
    });
    const item = makeItem({ run_status: "queued", run_id: "run-pending" });
    const { fetchMock } = renderReview(makeReview([item]), () => pendingPost);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", {
        name: "Arrêter cette tentative",
      }),
    );
    expect(screen.getByRole("button", { name: "Arrêt…" })).toBeDisabled();

    resolvePost(
      Response.json(
        { detail: { code: "production_run_not_cancellable" } },
        { status: 409 },
      ),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "La revue de publication n’a pas pu être mise à jour.",
    );
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(1);
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
    expect(
      screen.getByRole("button", { name: /Articles bloquants/ }),
    ).toHaveTextContent("1");
    expect(
      screen.getByRole("button", { name: /Sources à fournir/ }),
    ).toHaveTextContent("0");
    expect(
      screen.getByText("Résolvez ou excluez les articles bloquants."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Accepter la production" }),
    ).toBeDisabled();
  });

  it("signale les pertes par des filtres locaux dans le Repair Desk", async () => {
    const signalled = makeItem({
      rejected_indicator_count: 7,
      rejected_rule_count: 2,
      published_rule_count: 5,
    });
    const silent = makeItem({
      subject_id: "subject-silent",
      title: "Article sans perte",
    });
    renderReview(makeReview([signalled, silent]));

    const signalledCard = (await screen.findAllByRole("listitem"))[0]!;
    expect(
      within(signalledCard).getByRole("button", {
        name: "2 règle(s) de détection à arbitrer",
      }),
    ).toBeInTheDocument();
    expect(
      within(signalledCard).getByRole("button", {
        name: "7 indicateur(s) à arbitrer",
      }),
    ).toBeInTheDocument();
    expect(
      within(signalledCard).getByText("5 règle(s) de détection publiée(s)"),
    ).toBeInTheDocument();

    const silentCard = (await screen.findAllByRole("listitem"))[1]!;
    expect(
      within(silentCard).queryByRole("button", {
        name: /règle\(s\) de détection|indicateur\(s\) écarté\(s\)/,
      }),
    ).not.toBeInTheDocument();
  });

  it("affiche les compteurs de réparation fournis par le Repair Desk", async () => {
    // Le calcul des pertes appartient désormais au contrat de réparation du backend.
    const excluded = makeItem({
      subject_id: "subject-excluded",
      title: "Article exclu",
      effective_decision: "exclude",
      included: false,
      rejected_indicator_count: 100,
      rejected_rule_count: 100,
      published_rule_count: 100,
    });
    renderReview(
      makeReview([
        makeItem({
          rejected_indicator_count: 3,
          rejected_rule_count: 2,
          published_rule_count: 4,
        }),
        excluded,
      ]),
    );

    expect(await screen.findByText("Autres pertes")).toBeInTheDocument();
    expect(
      screen.queryByText(/Sur l’ensemble de l’édition/),
    ).not.toBeInTheDocument();
  });

  it("active Accept sans body métier et invalide la publication et l’édition", async () => {
    const { client, fetchMock } = renderReview(makeReview());
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const user = userEvent.setup();

    const acceptButton = await screen.findByRole("button", {
      name: "Accepter la production",
    });
    await waitFor(() => expect(acceptButton).toBeEnabled());
    await user.click(acceptButton);

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

    const acceptButton = await screen.findByRole("button", {
      name: "Accepter la production",
    });
    await waitFor(() => expect(acceptButton).toBeEnabled());
    await user.click(acceptButton);

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
  it("propose la réconciliation ChatGPT au lieu d’un retry générique", async () => {
    renderReview(makeReview([reconciliationItem()]));

    await screen.findByText("Article de test");
    expect(
      screen.getByRole("button", { name: "Récupérer la réponse ChatGPT" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Réessayer" }),
    ).not.toBeInTheDocument();
    // L’identité exacte est affichée, jamais devinée depuis le message.
    expect(screen.getByText("model-run-1")).toBeInTheDocument();
    expect(screen.getByText("bridge-1")).toBeInTheDocument();
  });

  it("preview puis confirmation adoptent le SHA-256 affiché et rechargent la revue", async () => {
    const item = reconciliationItem();
    const preview = {
      production_run_id: item.run_id,
      model_run_id: "model-run-1",
      stage: "synthesis",
      pipeline_generation: 3,
      bridge_response_id: "bridge-1",
      submission_state: "submitted_or_unknown",
      phase: "reconciliation",
      text: "# réponse récupérée",
      sha256: "c".repeat(64),
      chars: 19,
      metadata: {},
      visible_available: true,
    };
    const { client, fetchMock } = renderReview(makeReview([item]), () =>
      Response.json(preview),
    );
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", {
        name: "Récupérer la réponse ChatGPT",
      }),
    );
    expect(await screen.findByText("c".repeat(64))).toBeInTheDocument();
    expect(screen.getByText("# réponse récupérée")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", {
        name: "Confirmer et reprendre la production",
      }),
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
      ).toHaveLength(2),
    );
    const posts = postCalls(fetchMock);
    expect(posts.map(([url]) => url)).toEqual([
      "/api/production/runs/run-reconcile/reconciliation/visible/preview",
      "/api/production/runs/run-reconcile/reconciliation/visible/adopt",
    ]);
    expect(JSON.parse(bodyOf(posts[1]![1]))).toEqual({
      expected_sha256: "c".repeat(64),
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["edition-review", EDITION_ID],
    });
  });

  it("bascule sur l’import Markdown quand la cible visible est perdue", async () => {
    const item = reconciliationItem();
    const manualPreview = {
      production_run_id: item.run_id,
      model_run_id: "model-run-1",
      stage: "synthesis",
      pipeline_generation: 3,
      bridge_response_id: null,
      submission_state: "submitted_or_unknown",
      phase: "reconciliation",
      text: "# collé",
      sha256: "d".repeat(64),
      chars: 7,
      metadata: { source: "manual_import" },
      visible_available: false,
    };
    const { fetchMock } = renderReview(makeReview([item]), () =>
      Response.json(manualPreview),
    );
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", {
        name: "Réponse ChatGPT indisponible ? Coller le Markdown",
      }),
    );
    await user.type(screen.getByLabelText("Réponse Markdown"), "# collé");
    await user.click(
      screen.getByRole("button", { name: "Prévisualiser l’import" }),
    );

    expect(await screen.findByText("d".repeat(64))).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: "Confirmer et reprendre la production",
      }),
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
      ).toHaveLength(2),
    );
    const posts = postCalls(fetchMock);
    expect(posts[1]![0]).toBe(
      "/api/production/runs/run-reconcile/reconciliation/manual/adopt",
    );
    expect(JSON.parse(bodyOf(posts[1]![1]))).toEqual({
      markdown: "# collé",
      expected_sha256: "d".repeat(64),
    });
  });

  it("n’offre jamais de retry pour un article annulé", async () => {
    renderReview(
      makeReview([
        makeItem({
          run_status: "cancelled",
          effective_decision: null,
          included: false,
          blocking: true,
          can_retry: false,
          retry_stage: null,
          document_artifact_id: null,
          document_artifact_version: null,
          document_input_hash: null,
        }),
      ]),
    );

    await screen.findByText("Article de test");
    expect(
      screen.queryByRole("button", { name: "Réessayer" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Récupération ChatGPT"),
    ).not.toBeInTheDocument();
    // Il reste exclusible, seule sortie cohérente avec le domaine.
    expect(screen.getByRole("button", { name: "Exclure" })).toBeInTheDocument();
  });
});
