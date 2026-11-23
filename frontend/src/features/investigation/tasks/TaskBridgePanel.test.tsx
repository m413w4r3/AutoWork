import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  bodyOf,
  createTestQueryClient,
  errorResponse,
  headersOf,
  installFetch,
  jsonResponse,
  wrapperFor,
} from "../api/testSupport";
import type {
  TaskConsultationResponseV1,
  TaskConsultationV1,
} from "../api/taskConsultations";
import { INVESTIGATION_ID, TASK_ID, makeTask } from "./testFixtures";
import { TaskBridgePanel } from "./TaskBridgePanel";

afterEach(() => {
  vi.unstubAllGlobals();
});

const INVESTIGATION_URL = `/api/investigations/${INVESTIGATION_ID}`;
const LEDGER_URL = `${INVESTIGATION_URL}/consultations?task_id=${TASK_ID}`;
const CONSULTATION_URL = `${INVESTIGATION_URL}/tasks/${TASK_ID}/consultations`;

function ledgerRow(
  overrides: Partial<TaskConsultationV1> = {},
): TaskConsultationV1 {
  return {
    invocation_id: "11111111-1111-4111-8111-111111111111",
    operation_key: "operator:digest:payload",
    consultation_type: "critique",
    invocation_status: "succeeded",
    requester_actor_kind: "human",
    requester_actor_id: "human:analyst",
    task_id: null,
    agent_session_id: null,
    context_task_id: TASK_ID,
    call_observation_id: null,
    response_observation_id: null,
    output_sha256: "a".repeat(64),
    parse_status: "parsed",
    readable: true,
    usable: true,
    advisory_only: true,
    editorial_target: null,
    is_evidence: false,
    extracted_elements: [],
    contributions: [],
    limitations: [],
    reconciliation_state: "not_required",
    created_at: "2026-11-23T09:00:00Z",
    updated_at: "2026-11-23T09:01:00Z",
    response_observed_at: "2026-11-23T09:01:00Z",
    ...overrides,
  };
}

function consultationResponse(
  consultation: TaskConsultationV1 = ledgerRow(),
): TaskConsultationResponseV1 {
  return {
    invocation_id: consultation.invocation_id,
    invocation_status: consultation.invocation_status,
    context_task_id: TASK_ID,
    task_context_sha256: "b".repeat(64),
    consultation,
  };
}

function renderPanel(task = makeTask()) {
  return render(
    <TaskBridgePanel investigationId={INVESTIGATION_ID} task={task} />,
    { wrapper: wrapperFor(createTestQueryClient()) },
  );
}

function standardHandler(options: {
  ledger?: TaskConsultationV1[];
  submit?: (init: RequestInit | undefined, index: number) => Response;
}) {
  let submits = 0;
  return ({ url, init }: { url: string; init: RequestInit | undefined }) => {
    const method = init?.method ?? "GET";
    if (url === INVESTIGATION_URL) {
      return jsonResponse({ version: 5 });
    }
    if (url === LEDGER_URL && method === "GET") {
      return jsonResponse({
        investigation_id: INVESTIGATION_ID,
        consultations: options.ledger ?? [],
      });
    }
    if (url === CONSULTATION_URL && method === "POST") {
      const index = submits;
      submits += 1;
      if (options.submit !== undefined) return options.submit(init, index);
      return jsonResponse(consultationResponse());
    }
    return errorResponse(500, "unexpected_request");
  };
}

async function openPanel(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Demander à Bridge" }));
  await screen.findByText(/Version de l’état utilisée pour le CAS : 5/);
}

describe("TaskBridgePanel", () => {
  it("décrit l’avis consultatif et les trois types de demande", async () => {
    const user = userEvent.setup();
    installFetch(standardHandler({}));
    renderPanel();
    await openPanel(user);

    expect(
      screen.getByText(/ni résultat de tâche, ni preuve, ni décision/),
    ).toBeInTheDocument();
    const select = screen.getByLabelText("Type de demande");
    expect(
      Array.from((select as HTMLSelectElement).options).map(
        (option) => option.value,
      ),
    ).toEqual(["interpretation", "critique", "pivot_strategy"]);
  });

  it("envoie la demande avec CAS, idempotence et affiche l’état reçu du registre", async () => {
    const user = userEvent.setup();
    const recorder = installFetch(standardHandler({}));
    renderPanel();
    await openPanel(user);

    await user.selectOptions(
      screen.getByLabelText("Type de demande"),
      "critique",
    );
    await user.type(
      screen.getByLabelText("Question pour Bridge"),
      "Le domaine est-il un C2 ?",
    );
    await user.click(
      screen.getByLabelText(/Inclure la consultation précédente/),
    );
    await user.click(
      screen.getByRole("button", { name: "Envoyer la demande à Bridge" }),
    );

    expect(await screen.findByText(/Demande : Reçue — Parsée/)).toBeVisible();
    const request = recorder.calls.find(
      (call) => call.url === CONSULTATION_URL && call.init?.method === "POST",
    );
    expect(request).toBeDefined();
    if (request === undefined) throw new Error("consultation was not sent");
    expect(bodyOf(request.init)).toEqual({
      consultation_type: "critique",
      question: "Le domaine est-il un C2 ?",
      include_previous_consultation: true,
      expected_working_state_version: 5,
    });
    expect(headersOf(request.init)["Idempotency-Key"]).toBeTruthy();
    expect(
      recorder.calls.some(
        (call) =>
          call.url.endsWith("/commands") || call.url.endsWith("/transfer"),
      ),
    ).toBe(false);
  });

  it("affiche les états lus du registre, y compris l’ambiguïté", async () => {
    const user = userEvent.setup();
    installFetch(
      standardHandler({
        ledger: [
          ledgerRow({
            invocation_id: "11111111-1111-4111-8111-111111111111",
            invocation_status: "running",
            parse_status: "unparsed",
            created_at: "2026-11-23T09:05:00Z",
          }),
          ledgerRow({
            invocation_id: "22222222-2222-4222-8222-222222222222",
            invocation_status: "succeeded",
            parse_status: "parsed",
            created_at: "2026-11-23T09:04:00Z",
          }),
          ledgerRow({
            invocation_id: "33333333-3333-4333-8333-333333333333",
            invocation_status: "succeeded",
            parse_status: "partially_parsed",
            created_at: "2026-11-23T09:03:00Z",
          }),
          ledgerRow({
            invocation_id: "44444444-4444-4444-8444-444444444444",
            invocation_status: "failed",
            parse_status: "unparsed",
            created_at: "2026-11-23T09:02:00Z",
          }),
          ledgerRow({
            invocation_id: "55555555-5555-4555-8555-555555555555",
            invocation_status: "needs_review",
            parse_status: "unparsed",
            reconciliation_state: "required",
            created_at: "2026-11-23T09:01:00Z",
          }),
        ],
      }),
    );
    renderPanel();
    await openPanel(user);

    expect(screen.getByText(/· Envoyée — en cours ·/)).toBeVisible();
    expect(screen.getByText(/· Reçue ·/)).toBeVisible();
    expect(screen.getByText(/· Partielle ·/)).toBeVisible();
    expect(screen.getByText(/· Refusée ·/)).toBeVisible();
    expect(screen.getByText(/· Ambiguë à réconcilier ·/)).toBeVisible();
  });

  it("après un échec réseau, affiche l’état inconnu et rejoue la même clé", async () => {
    const user = userEvent.setup();
    const keys: Array<string | null | undefined> = [];
    const bodies: unknown[] = [];
    installFetch(
      standardHandler({
        submit: (init, index) => {
          keys.push(headersOf(init)["Idempotency-Key"]);
          bodies.push(bodyOf(init));
          if (index === 0) throw new TypeError("Failed to fetch");
          return jsonResponse(consultationResponse());
        },
      }),
    );
    renderPanel();
    await openPanel(user);

    await user.type(
      screen.getByLabelText("Question pour Bridge"),
      "Le domaine est-il un C2 ?",
    );
    await user.click(
      screen.getByRole("button", { name: "Envoyer la demande à Bridge" }),
    );

    expect(await screen.findByText("État inconnu.")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Vérifier le registre" }),
    );
    expect(await screen.findByText(/Registre rafraîchi/)).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Réessayer avec la même clé" }),
    );

    expect(await screen.findByText(/Demande : Reçue — Parsée/)).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
    expect(bodies[1]).toEqual(bodies[0]);
    expect(bodies[0]).toEqual({
      consultation_type: "interpretation",
      question: "Le domaine est-il un C2 ?",
      include_previous_consultation: false,
      expected_working_state_version: 5,
    });
  });

  it("traite un refus typé sans état inconnu et repart d’une nouvelle clé", async () => {
    const user = userEvent.setup();
    const keys: Array<string | null | undefined> = [];
    installFetch(
      standardHandler({
        submit: (init, index) => {
          keys.push(headersOf(init)["Idempotency-Key"]);
          if (index === 0) {
            return errorResponse(403, "external_disclosure_denied");
          }
          return jsonResponse(consultationResponse());
        },
      }),
    );
    renderPanel();
    await openPanel(user);

    await user.type(
      screen.getByLabelText("Question pour Bridge"),
      "Le domaine est-il un C2 ?",
    );
    await user.click(
      screen.getByRole("button", { name: "Envoyer la demande à Bridge" }),
    );

    expect(await screen.findByText(/G2 refusé/)).toBeVisible();
    expect(screen.queryByText("État inconnu.")).toBeNull();
    await user.click(
      screen.getByRole("button", { name: "Envoyer la demande à Bridge" }),
    );
    expect(await screen.findByText(/Demande : Reçue — Parsée/)).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBeTruthy();
    expect(keys[1]).not.toBe(keys[0]);
  });

  it("désactive l’envoi d’une tâche terminale mais garde le registre lisible", async () => {
    const user = userEvent.setup();
    installFetch(
      standardHandler({ ledger: [ledgerRow({ invocation_status: "failed" })] }),
    );
    renderPanel(makeTask({ status: "completed" }));
    await openPanel(user);

    await user.type(
      screen.getByLabelText("Question pour Bridge"),
      "Peut-on encore demander ?",
    );
    expect(
      screen.getByRole("button", { name: "Envoyer la demande à Bridge" }),
    ).toBeDisabled();
    expect(
      screen.getByText(
        /Tâche terminale : une consultation n’est plus acceptée/,
      ),
    ).toBeVisible();
    expect(screen.getByText(/Refusée/)).toBeVisible();
  });

  it("ouvre la vue Avis avec la tâche pré-remplie et la vue Plan avec son contexte", async () => {
    const user = userEvent.setup();
    installFetch(standardHandler({}));
    renderPanel();
    await openPanel(user);

    const draft = screen.getByRole("link", {
      name: "Rédiger une Note avec Bridge",
    });
    const review = screen.getByRole("link", {
      name: "Corriger une Note avec Bridge",
    });
    const plan = screen.getByRole("link", {
      name: "Réviser le plan Bridge avec cette tâche",
    });
    const draftUrl = new URL(draft.getAttribute("href") ?? "", "http://local");
    expect(draftUrl.searchParams.get("vue")).toBe("opinion");
    expect(draftUrl.searchParams.get("task_id")).toBe(TASK_ID);
    expect(draftUrl.searchParams.get("bridge_intent")).toBe("draft_text");
    const reviewUrl = new URL(
      review.getAttribute("href") ?? "",
      "http://local",
    );
    expect(reviewUrl.searchParams.get("bridge_intent")).toBe("review_text");
    const planUrl = new URL(plan.getAttribute("href") ?? "", "http://local");
    expect(planUrl.searchParams.get("vue")).toBe("plan");
    expect(planUrl.searchParams.get("context_task_id")).toBe(TASK_ID);
  });

  it("nettoie les paramètres de l’autre vue dans les liens", async () => {
    window.history.pushState(
      {},
      "",
      `/?vue=plan&task_id=previous&context_task_id=${TASK_ID}&bridge_intent=draft_text`,
    );
    try {
      const user = userEvent.setup();
      installFetch(standardHandler({}));
      renderPanel();
      await openPanel(user);

      const draft = screen.getByRole("link", {
        name: "Rédiger une Note avec Bridge",
      });
      const draftUrl = new URL(
        draft.getAttribute("href") ?? "",
        "http://local",
      );
      expect(draftUrl.searchParams.get("context_task_id")).toBeNull();
      const plan = screen.getByRole("link", {
        name: "Réviser le plan Bridge avec cette tâche",
      });
      const planUrl = new URL(plan.getAttribute("href") ?? "", "http://local");
      expect(planUrl.searchParams.get("task_id")).toBeNull();
      expect(planUrl.searchParams.get("bridge_intent")).toBeNull();
    } finally {
      window.history.pushState({}, "", "/");
    }
  });
});
