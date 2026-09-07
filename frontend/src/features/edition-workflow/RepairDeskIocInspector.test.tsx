import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditionRepairDetail,
  EditionRepairItem,
  EditionRepairPage,
  EditionReview,
  RepairExecutionPlan,
  ReviewItem,
} from "../../api/publication";
import { ReviewConsole } from "./ReviewConsole";

const EDITION_ID = "edition-ioc-inspector";
const HASH = "a".repeat(64);
const IOC_VALUE = "b".repeat(64);
const CORRECTED_VALUE = "c".repeat(64);

/** The plan an IOC arbitration must advertise: publication only, no model. */
const IOC_PLAN: RepairExecutionPlan = {
  impact_kind: "publication_only",
  affected_outputs: ["extraction", "publication", "checkpoint"],
  model_call_required: false,
  provider_steps: [],
  deterministic_steps: [
    "Décision analyste",
    "Projection Extraction",
    "Rendu Publication",
    "Contrôle QA",
  ],
  ready_to_apply: true,
};

const reviewItem: ReviewItem = {
  position: 1,
  subject_id: "subject-1",
  title: "Article IOC",
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
  rejected_indicator_count: 1,
  rejected_rule_count: 0,
  published_rule_count: 0,
  can_retry: false,
  retry_stage: null,
  requires_reconciliation: false,
  reconciliation: null,
  error_code: null,
  error_message: null,
};

function iocItem(
  overrides: Partial<EditionRepairItem> = {},
): EditionRepairItem {
  return {
    repair_key: "repair-ioc-hash",
    kind: "rejected_indicator",
    position: 1,
    subject_id: "subject-1",
    article_title: "Article IOC",
    run_id: "run-1",
    pipeline_generation: 4,
    artifact_id: "artifact-ioc-1",
    artifact_version: 1,
    source_id: "S6",
    source_title: "Rapport de menace",
    source_url: "https://source.example/report",
    collection_id: null,
    collection_state: "archived",
    artifact_type: "hash",
    preview: IOC_VALUE,
    reason_code: "source_evidence_not_text_verifiable",
    value_sha256: HASH,
    payload_available: true,
    effective_action: null,
    effective_decision_id: null,
    resolved: false,
    resolution_reason: null,
    rebuild_required: false,
    execution_plan: IOC_PLAN,
    recommended_stage: null,
    is_publication_ioc: true,
    application_state: "unresolved",
    ...overrides,
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
    execution_plan: item.execution_plan,
  };
}

function repairPage(
  items: EditionRepairItem[],
  articles: EditionRepairPage["articles"] = [],
): EditionRepairPage {
  return {
    summary: {
      unresolved_total: items.filter((item) => !item.resolved).length,
      sources_to_supply: 0,
      rejected_iocs_to_review: items.filter((item) => !item.resolved).length,
      rejected_rules_to_review: 0,
      rejected_other_artifacts: 0,
      articles_with_repairs: 1,
      articles_needing_rebuild: articles.length,
    },
    items,
    articles,
    next_cursor: null,
  };
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function renderDesk(
  page: EditionRepairPage,
  handlers: {
    rebuild?: () => Response;
    verify?: () => Response;
  } = {},
) {
  const review: EditionReview = {
    edition_id: EDITION_ID,
    items: [reviewItem],
    can_accept: true,
  };
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = urlOf(input);
    if (init?.method === "POST") {
      if (url.includes("/review/items/") && url.endsWith("/rebuild")) {
        return Promise.resolve(
          handlers.rebuild?.() ??
            Response.json({
              action: "publication_reassembled",
              stage: "none",
              run_id: "run-1",
              batch_id: null,
              changed: true,
              job_id: null,
            }),
        );
      }
      if (url.endsWith("/verify-replacement")) {
        return Promise.resolve(
          handlers.verify?.() ??
            Response.json({
              verified: true,
              format_valid: true,
              normalized_value: CORRECTED_VALUE,
              artifact_type: "hash",
              source_id: "S6",
              source_url: "https://source.example/report",
              reason_code: null,
              verification_state: "source_verified",
              context_spans: [{ kind: "body_text", text: CORRECTED_VALUE }],
            }),
        );
      }
      if (url.includes("/review/repairs/") && url.endsWith("/decision")) {
        return Promise.resolve(
          Response.json({
            repair_key: "repair-ioc-hash",
            decision_id: "decision-1",
            action: "replace",
            resolved: true,
          }),
        );
      }
    }
    if (url.includes("/review/repairs?"))
      return Promise.resolve(Response.json(page));
    if (url.includes("/review/repairs/")) {
      const key = url.split("/repairs/")[1] ?? "";
      const item = page.items.find((candidate) => candidate.repair_key === key);
      return Promise.resolve(
        Response.json(detailFor(item ?? page.items[0] ?? iocItem())),
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
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Repair Desk — inspecteur IOC", () => {
  it("affiche la valeur, le type, la source et le motif du rejet", async () => {
    renderDesk(repairPage([iocItem()]));
    const user = userEvent.setup();

    await user.click(await screen.findByText(IOC_VALUE));

    const inspector = await screen.findByRole("region", {
      name: "Article IOC",
    });
    expect(inspector).toHaveTextContent("hash");
    expect(inspector).toHaveTextContent(IOC_VALUE);
    expect(inspector).toHaveTextContent("S6");
    expect(inspector).toHaveTextContent("Rapport de menace");
    // Le plan reste chirurgical : aucune synthèse, aucun appel modèle.
    expect(inspector).toHaveTextContent("Publication uniquement");
    expect(inspector).toHaveTextContent("Aucun appel modèle");
  });

  it("offre inclure, corriger la valeur et exclure sans imposer de raison", async () => {
    renderDesk(repairPage([iocItem()]));
    const user = userEvent.setup();

    await user.click(await screen.findByText(IOC_VALUE));

    const inspector = await screen.findByRole("region", {
      name: "Article IOC",
    });
    const actions = within(inspector);
    expect(
      await actions.findByRole("button", { name: "Inclure dans la fiche" }),
    ).toBeEnabled();
    expect(
      actions.getByRole("button", { name: "Corriger la valeur" }),
    ).toBeEnabled();
    expect(actions.getByRole("button", { name: "Exclure" })).toBeEnabled();
    // La note d’audit existe, mais repliée et explicitement facultative.
    expect(actions.getByText(/facultatif/i)).toBeInTheDocument();
  });

  it("corrige une valeur et envoie la valeur remplacée", async () => {
    const fetchMock = renderDesk(repairPage([iocItem()]));
    const user = userEvent.setup();

    await user.click(await screen.findByText(IOC_VALUE));
    const inspector = await screen.findByRole("region", {
      name: "Article IOC",
    });
    await user.click(
      await within(inspector).findByRole("button", {
        name: "Corriger la valeur",
      }),
    );
    const field = await screen.findByLabelText("Valeur corrigée");
    await user.type(field, CORRECTED_VALUE);
    await user.click(
      screen.getByRole("button", { name: "Vérifier dans la source" }),
    );

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            urlOf(input).endsWith("/verify-replacement") &&
            init?.method === "POST",
        ),
      ).toBe(true),
    );
    const [, verifyInit] =
      fetchMock.mock.calls.find(([input]) =>
        urlOf(input).endsWith("/verify-replacement"),
      ) ?? [];
    const sentBody =
      typeof verifyInit?.body === "string" ? verifyInit.body : "";
    expect(sentBody).toContain(CORRECTED_VALUE);
  });

  it("rend visible le diagnostic d’une application refusée", async () => {
    const articles: EditionRepairPage["articles"] = [
      {
        subject_id: "subject-1",
        has_pending_projection: true,
        execution_plan: IOC_PLAN,
        recommended_stage: "apply_projection",
        active_repair_count: 1,
        resolved_since_last_build_count: 1,
      },
    ];
    renderDesk(
      repairPage(
        [iocItem({ resolved: true, effective_action: "include" })],
        articles,
      ),
      {
        rebuild: () =>
          Response.json(
            {
              detail: {
                code: "repair_payload_unavailable",
                message: "repair_payload_unavailable",
                repair_id: "subject-1",
                stage: "payload",
                error_code: "repair_payload_unavailable",
                remediation: "rerun_extraction",
              },
            },
            { status: 409 },
          ),
      },
    );
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", {
        name: "Mettre à jour la publication",
      }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Récupération de la valeur");
    expect(alert).toHaveTextContent(/Relancez l’étape Extraction/);
    expect(alert).toHaveTextContent("repair_payload_unavailable");
  });
});
