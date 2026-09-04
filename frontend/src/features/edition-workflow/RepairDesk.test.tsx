import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditionRepairDetail,
  EditionRepairItem,
  EditionRepairPage,
  EditionReview,
  ReviewItem,
} from "../../api/publication";
import { ReviewConsole } from "./ReviewConsole";

const EDITION_ID = "edition-repair-test";
const HASH = "a".repeat(64);

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function jsonObject(init: RequestInit | undefined): Record<string, unknown> {
  if (typeof init?.body !== "string") {
    throw new Error("Expected a JSON request body");
  }
  const parsed: unknown = JSON.parse(init.body);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("Expected a JSON object");
  }
  return parsed as Record<string, unknown>;
}

function repairAction(
  value: unknown,
): "include" | "exclude" | "continue_without_source" {
  if (
    value === "include" ||
    value === "exclude" ||
    value === "continue_without_source"
  ) {
    return value;
  }
  throw new Error("Unexpected repair action");
}

const reviewItem: ReviewItem = {
  position: 1,
  subject_id: "subject-1",
  title: "Article audit",
  run_id: "run-1",
  pipeline_generation: 4,
  run_status: "ready",
  document_artifact_id: "document-1",
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

function repairItem(
  overrides: Partial<EditionRepairItem> = {},
): EditionRepairItem {
  return {
    repair_key: "repair-ioc-1",
    kind: "rejected_indicator",
    position: 1,
    subject_id: "subject-1",
    article_title: "Article audit",
    run_id: "run-1",
    pipeline_generation: 4,
    artifact_id: "artifact-ioc-1",
    artifact_version: 1,
    source_id: "S6",
    source_title: "Rapport de menace",
    source_url: "https://source.example/report",
    collection_id: null,
    collection_state: "archived",
    artifact_type: "domain",
    preview: "evil.example",
    reason_code: "source_evidence_not_text_verifiable",
    value_sha256: HASH,
    payload_available: true,
    effective_action: null,
    effective_decision_id: null,
    resolved: false,
    resolution_reason: null,
    rebuild_required: false,
    recommended_stage: null,
    is_publication_ioc: true,
    ...overrides,
  };
}

function page(
  items: EditionRepairItem[],
  overrides: Partial<EditionRepairPage["summary"]> = {},
  articles: EditionRepairPage["articles"] = [],
  next_cursor: string | null = null,
): EditionRepairPage {
  return {
    summary: {
      unresolved_total: items.filter((item) => !item.resolved).length,
      sources_to_supply: items.filter(
        (item) =>
          !item.resolved && item.kind === "supplemental_source_unarchived",
      ).length,
      rejected_iocs_to_review: items.filter(
        (item) => !item.resolved && item.kind === "rejected_indicator",
      ).length,
      rejected_rules_to_review: items.filter(
        (item) => !item.resolved && item.kind === "rejected_rule",
      ).length,
      rejected_other_artifacts: 0,
      articles_with_repairs: 1,
      articles_needing_rebuild: 0,
      ...overrides,
    },
    items,
    articles,
    next_cursor,
  };
}

function detailFor(item: EditionRepairItem): EditionRepairDetail {
  return {
    repair_key: item.repair_key,
    kind: item.kind,
    artifact_id: item.artifact_id,
    artifact_version: item.artifact_version,
    source_id: item.source_id,
    source_title: item.source_title,
    source_url: item.source_url,
    artifact_type: item.artifact_type,
    reason_code: item.reason_code,
    value_sha256: item.value_sha256,
    preview: item.preview,
    payload_available: item.payload_available,
    value: item.preview,
    body: null,
    collection_id: item.collection_id,
    collection_state: item.collection_state,
    effective_decision: null,
  };
}

function renderReview(
  repairPage: EditionRepairPage,
  details: ReadonlyMap<string, EditionRepairDetail> = new Map(
    repairPage.items.map((item) => [item.repair_key, detailFor(item)]),
  ),
  review: EditionReview = {
    edition_id: EDITION_ID,
    items: [reviewItem],
    can_accept: true,
  },
  nextPage?: EditionRepairPage,
) {
  const currentPage = { value: repairPage };
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    if (init?.method === "POST") {
      if (url.includes("/review/repairs/") && url.endsWith("/decision")) {
        const body = jsonObject(init);
        const action = repairAction(body["action"]);
        const repairKey = url.split("/repairs/")[1]?.split("/")[0];
        if (repairKey) {
          const updated = currentPage.value.items.map((item) =>
            item.repair_key === repairKey
              ? {
                  ...item,
                  resolved: true,
                  effective_action: action,
                  effective_decision_id: "decision-1",
                }
              : item,
          );
          currentPage.value = page(updated, {
            unresolved_total: updated.filter((item) => !item.resolved).length,
          });
        }
        return Promise.resolve(
          Response.json({
            repair_key: repairKey,
            decision_id: "decision-1",
            action,
            resolved: true,
          }),
        );
      }
      if (url.includes("/review/repairs/decisions")) {
        return Promise.resolve(
          Response.json({ decision_ids: ["decision-1"], decisions: [] }),
        );
      }
      if (url.includes("/review/items/") && url.endsWith("/rebuild")) {
        return Promise.resolve(
          Response.json({
            action: "rebuild_started",
            stage: "references",
            run_id: "run-rebuild",
            batch_id: "batch-rebuild",
            changed: true,
            job_id: "job-rebuild",
          }),
        );
      }
      if (url.includes("/sources/") && url.endsWith("/content")) {
        return Promise.resolve(Response.json({ state: "archived" }));
      }
      if (url.endsWith("/publication/accept")) {
        return Promise.resolve(Response.json({ edition_id: EDITION_ID }));
      }
    }
    if (url.includes("/review/repairs?") || url.includes("/review/repairs?")) {
      return Promise.resolve(
        Response.json(
          url.includes("cursor=cursor-2") && nextPage
            ? nextPage
            : currentPage.value,
        ),
      );
    }
    if (url.includes("/review/repairs/")) {
      const key = url.split("/repairs/")[1] ?? "";
      return Promise.resolve(
        Response.json(
          details.get(key) ?? detailFor(currentPage.value.items[0]!),
        ),
      );
    }
    if (url.includes("/workbench")) {
      return Promise.resolve(
        Response.json({
          subject_id: "subject-1",
          sources: [
            {
              id: "collection-1",
              requested_url: "https://source.example/report",
              title: "Rapport de menace",
              publisher: "Publisher test",
            },
          ],
          claims: [],
          indicators: [],
        }),
      );
    }
    if (url.endsWith("/review")) return Promise.resolve(Response.json(review));
    return Promise.resolve(Response.json({}));
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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Repair Desk", () => {
  it("affiche le résumé, filtre localement et ouvre l’inspecteur", async () => {
    const ioc = repairItem();
    const source = repairItem({
      repair_key: "source-1",
      kind: "supplemental_source_unarchived",
      artifact_id: "artifact-source-1",
      source_id: "Q1",
      source_title: "Source proposée",
      preview: "https://source.example/blocked",
      reason_code: "supplemental_source_unarchived",
      collection_id: "collection-1",
      collection_state: "unavailable",
      is_publication_ioc: false,
    });
    renderReview(
      page([ioc, source], {
        unresolved_total: 2,
        sources_to_supply: 1,
        rejected_iocs_to_review: 1,
      }),
    );

    expect(
      await screen.findByText(/0 \/ 2 éléments arbitrés/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Sources à fournir/ }),
    ).toHaveTextContent("1");
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: /IOC à arbitrer/ }));
    expect(screen.getByText("evil.example")).toBeInTheDocument();
    expect(screen.queryByText("Source proposée")).not.toBeInTheDocument();

    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: /evil\.example/ }));
    expect(
      await screen.findByRole("heading", { name: "Article audit" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "https://source.example/report" }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Article audit" })).getByText(
        "La valeur n'a pas pu être vérifiée dans le texte archivé.",
      ),
    ).toBeInTheDocument();
  });

  it.each([
    ["include", "Inclure dans la fiche"],
    ["exclude", "Exclure"],
  ] as const)("enregistre une décision IOC %s", async (action, buttonName) => {
    const item = repairItem();
    const { fetchMock } = renderReview(page([item]));
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: /evil\.example/ }),
    );
    const inspector = await screen.findByRole("region", {
      name: "Article audit",
    });
    await user.click(
      within(inspector).getByRole("button", { name: buttonName }),
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            urlOf(input).includes("/review/repairs/repair-ioc-1/decision") &&
            init?.method === "POST",
        ),
      ).toBe(true),
    );
    const post = fetchMock.mock.calls.find(
      ([input, init]) =>
        urlOf(input).includes("/review/repairs/repair-ioc-1/decision") &&
        init?.method === "POST",
    );
    expect(jsonObject(post?.[1])).toMatchObject({
      action,
      observed_subject_id: "subject-1",
      observed_run_id: "run-1",
      observed_artifact_id: "artifact-ioc-1",
      observed_pipeline_generation: 4,
    });
    if (action === "include") {
      expect(
        await screen.findByText("Inclus par décision analyste"),
      ).toBeInTheDocument();
    }
  });

  it("confirme une action groupée avec le nombre d’éléments", async () => {
    const first = repairItem();
    const second = repairItem({
      repair_key: "repair-ioc-2",
      preview: "10.0.0.2",
      artifact_id: "artifact-ioc-2",
    });
    const { fetchMock } = renderReview(page([first, second]));
    const user = userEvent.setup();
    await screen.findByText("10.0.0.2");
    const checkboxes = screen.getAllByRole("checkbox");
    await user.click(checkboxes[0]!);
    await user.click(checkboxes[1]!);
    await user.click(
      screen.getByRole("button", { name: "Inclure 2 éléments" }),
    );
    expect(
      screen.getByText("Confirmer l'inclusion de 2 éléments ?"),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: "Confirmer l'inclusion de 2 éléments",
      }),
    );
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            urlOf(input).includes("/review/repairs/decisions") &&
            init?.method === "POST",
        ),
      ).toBe(true),
    );
    const post = fetchMock.mock.calls.find(
      ([input, init]) =>
        urlOf(input).includes("/review/repairs/decisions") &&
        init?.method === "POST",
    );
    expect(jsonObject(post?.[1])["decisions"]).toHaveLength(2);
  });

  it("rend la source disponible même en READY, archive du contenu puis permet le waive", async () => {
    const item = repairItem({
      repair_key: "source-1",
      kind: "supplemental_source_unarchived",
      artifact_id: "artifact-source-1",
      source_id: "Q1",
      source_title: "Source proposée",
      source_url: "https://source.example/blocked",
      preview: "https://source.example/blocked",
      reason_code: "supplemental_source_unarchived",
      collection_id: "collection-1",
      collection_state: "unavailable",
      is_publication_ioc: false,
    });
    const { fetchMock } = renderReview(page([item]));
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: /source\.example\/blocked/ }),
    );
    expect(
      await screen.findByRole("heading", { name: "Source proposée par Q1" }),
    ).toBeInTheDocument();
    const sourcePanel = screen.getByRole("region", {
      name: "Source proposée par Q1",
    });
    expect(
      within(sourcePanel).getByRole("link", {
        name: "https://source.example/blocked",
      }),
    ).toBeInTheDocument();
    await user.type(
      screen.getByLabelText("Coller le contenu"),
      "<html>source</html>",
    );
    await user.click(
      screen.getByRole("button", { name: "Archiver cette source" }),
    );
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            urlOf(input).includes("/sources/collection-1/content") &&
            init?.method === "POST",
        ),
      ).toBe(true),
    );
    expect(
      await screen.findByText(
        "Source archivée — reconstruction des références nécessaire.",
      ),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Continuer sans cette source" }),
    );
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            urlOf(input).includes("/review/repairs/source-1/decision") &&
            init?.method === "POST",
        ),
      ).toBe(true),
    );
  });

  it("charge le corps d’une règle uniquement après sélection et le rend comme texte", async () => {
    const rule = repairItem({
      repair_key: "rule-1",
      kind: "rejected_rule",
      artifact_id: "artifact-rule-1",
      artifact_type: "YARA",
      preview: "rule EvilExample",
      reason_code: "source_rule_evidence_missing",
      is_publication_ioc: false,
    });
    const ruleDetail = {
      ...detailFor(rule),
      body: "rule EvilExample { condition: <script>alert(1)</script> }",
    };
    const { fetchMock } = renderReview(
      page([rule]),
      new Map([[rule.repair_key, ruleDetail]]),
    );
    await screen.findByText("rule EvilExample");
    expect(
      fetchMock.mock.calls.some(([input]) =>
        urlOf(input).includes("/review/repairs/rule-1"),
      ),
    ).toBe(false);
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: /rule EvilExample/ }));
    expect(await screen.findByText(ruleDetail.body)).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
  });

  it("permet d’atteindre la page suivante", async () => {
    const first = repairItem();
    const second = repairItem({
      repair_key: "repair-ioc-201",
      preview: "last.example",
    });
    const firstPage = page([first], {}, [], "cursor-2");
    const secondPage = page([second]);
    const { fetchMock } = renderReview(
      firstPage,
      undefined,
      undefined,
      secondPage,
    );
    const user = userEvent.setup();
    expect(
      await screen.findByRole("button", { name: "Charger la suite" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Charger la suite" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) =>
          urlOf(input).includes("cursor=cursor-2"),
        ),
      ).toBe(true),
    );
    expect(await screen.findByText("last.example")).toBeInTheDocument();
  });

  it("reconstruit un article à l’unité ou plusieurs articles", async () => {
    const articles = [
      {
        subject_id: "subject-1",
        has_pending_projection: false,
        recommended_stage: "references",
        active_repair_count: 0,
        resolved_since_last_build_count: 1,
      },
      {
        subject_id: "subject-2",
        has_pending_projection: true,
        recommended_stage: "synthesis",
        active_repair_count: 0,
        resolved_since_last_build_count: 0,
      },
    ];
    const review: EditionReview = {
      edition_id: EDITION_ID,
      items: [reviewItem],
      can_accept: true,
    };
    const { fetchMock } = renderReview(
      page([], { articles_needing_rebuild: 2 }, articles),
      new Map(),
      review,
    );
    const user = userEvent.setup();
    expect(
      await screen.findByText(/Références → Extraction/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Synthèse → Assemblage/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Accepter la production" }),
    ).toBeDisabled();
    expect(
      screen.getByText(
        "2 articles doivent être reconstruits avant finalisation.",
      ),
    ).toBeInTheDocument();
    await user.click(
      screen.getAllByRole("button", { name: "Reconstruire cet article" })[0]!,
    );
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            urlOf(input).includes("/review/items/subject-1/rebuild") &&
            init?.method === "POST",
        ),
      ).toBe(true),
    );

    cleanup();
    vi.unstubAllGlobals();
    const multiple = renderReview(
      page([], { articles_needing_rebuild: 2 }, articles),
      new Map(),
      review,
    );
    const multipleUser = userEvent.setup();
    await multipleUser.click(
      await screen.findByRole("button", { name: "Reconstruire 2 articles" }),
    );
    await waitFor(() =>
      expect(
        multiple.fetchMock.mock.calls.filter(
          ([input, init]) =>
            urlOf(input).includes("/review/items/") && init?.method === "POST",
        ),
      ).toHaveLength(2),
    );
  });

  it("explique pourquoi l’acceptation reste désactivée", async () => {
    const unresolved = repairItem();
    renderReview(page([unresolved], { unresolved_total: 1 }));
    const acceptButton = await screen.findByRole("button", {
      name: "Accepter la production",
    });
    await waitFor(() => expect(acceptButton).toBeDisabled());
    expect(
      screen.getByText(/La revue technique n’est pas terminée/),
    ).toBeInTheDocument();
  });

  it("signale une mutation obsolète et recharge la file", async () => {
    const item = repairItem();
    const { fetchMock } = renderReview(page([item]));
    const decisionUrl = "/review/repairs/repair-ioc-1/decision";
    fetchMock.mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = urlOf(input);
        if (url.includes(decisionUrl) && init?.method === "POST") {
          return Promise.resolve(
            Response.json(
              { detail: { code: "production_repair_stale" } },
              { status: 409 },
            ),
          );
        }
        return Promise.resolve(Response.json(page([item])));
      },
    );
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: /evil\.example/ }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Inclure dans la fiche" }),
    );
    expect(
      await screen.findByText(
        "Cet élément a changé depuis son ouverture. La file de réparation a été rechargée.",
      ),
    ).toBeInTheDocument();
  });
});
