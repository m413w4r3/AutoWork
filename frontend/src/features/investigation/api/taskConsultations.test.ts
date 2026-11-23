import { afterEach, describe, expect, it, vi } from "vitest";

import {
  deriveTaskConsultationState,
  listTaskConsultations,
  requestTaskConsultation,
  TASK_CONSULTATION_STATE_LABELS,
  type TaskConsultationV1,
} from "./taskConsultations";
import {
  bodyOf,
  headersOf,
  installFetch,
  jsonResponse,
  lastRequest,
} from "./testSupport";

afterEach(() => {
  vi.unstubAllGlobals();
});

function consultation(
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
    context_task_id: "22222222-2222-4222-8222-222222222222",
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

describe("task consultation reads", () => {
  it("filters the ledger on the task context", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ investigation_id: "inv-1", consultations: [] }),
    );

    await listTaskConsultations("inv-1", "task-1");

    expect(lastRequest(recorder).url).toBe(
      "/api/investigations/inv-1/consultations?task_id=task-1",
    );
    expect(lastRequest(recorder).init?.method).toBe("GET");
  });
});

describe("task consultation requests", () => {
  it("posts the bounded body with the working-state CAS and the key", async () => {
    const recorder = installFetch(() =>
      jsonResponse({ invocation_id: "invocation-1" }),
    );

    await requestTaskConsultation(
      "inv-1",
      "task-1",
      {
        consultation_type: "pivot_strategy",
        question: "Quelles pistes de pivot après ce domaine ?",
        include_previous_consultation: true,
        expected_working_state_version: 7,
      },
      "idem-task-consultation-1",
    );

    const request = lastRequest(recorder);
    expect(request.url).toBe(
      "/api/investigations/inv-1/tasks/task-1/consultations",
    );
    expect(request.init?.method).toBe("POST");
    expect(headersOf(request.init)["Idempotency-Key"]).toBe(
      "idem-task-consultation-1",
    );
    expect(bodyOf(request.init)).toEqual({
      consultation_type: "pivot_strategy",
      question: "Quelles pistes de pivot après ce domaine ?",
      include_previous_consultation: true,
      expected_working_state_version: 7,
    });
  });
});

describe("displayed state derived from the registry row", () => {
  const cases: Array<{
    label: string;
    overrides: Partial<TaskConsultationV1>;
    expected: keyof typeof TASK_CONSULTATION_STATE_LABELS;
  }> = [
    {
      label: "en attente",
      overrides: { invocation_status: "pending" },
      expected: "sent",
    },
    {
      label: "en cours",
      overrides: { invocation_status: "running" },
      expected: "sent",
    },
    {
      label: "réussie et parsée",
      overrides: { invocation_status: "succeeded", parse_status: "parsed" },
      expected: "received",
    },
    {
      label: "réussie mais partiellement parsée",
      overrides: {
        invocation_status: "succeeded",
        parse_status: "partially_parsed",
      },
      expected: "partial",
    },
    {
      label: "réussie mais non parsée",
      overrides: { invocation_status: "succeeded", parse_status: "unparsed" },
      expected: "partial",
    },
    {
      label: "partielle",
      overrides: { invocation_status: "partial", parse_status: "unparsed" },
      expected: "partial",
    },
    {
      label: "vide",
      overrides: { invocation_status: "empty", parse_status: "unparsed" },
      expected: "partial",
    },
    {
      label: "échouée",
      overrides: { invocation_status: "failed", parse_status: "unparsed" },
      expected: "refused",
    },
    {
      label: "refusée par la politique",
      overrides: {
        invocation_status: "policy_denied",
        parse_status: "unparsed",
      },
      expected: "refused",
    },
    {
      label: "indisponible",
      overrides: { invocation_status: "unavailable", parse_status: "unparsed" },
      expected: "refused",
    },
    {
      label: "revue humaine requise",
      overrides: {
        invocation_status: "needs_review",
        parse_status: "unparsed",
      },
      expected: "ambiguous",
    },
    {
      label: "réconciliation requise malgré un succès",
      overrides: { reconciliation_state: "required" },
      expected: "ambiguous",
    },
  ];

  it.each(cases)("$label → $expected", ({ overrides, expected }) => {
    expect(deriveTaskConsultationState(consultation(overrides))).toBe(expected);
  });
});
