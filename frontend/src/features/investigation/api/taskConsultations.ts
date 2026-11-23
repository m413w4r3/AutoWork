/**
 * Task-scoped Bridge consultations (`api/investigation_task_consultations.py`,
 * M24.1 F4-D B3).
 *
 * `POST …/tasks/{task_id}/consultations` reuses the canonical `advisor.consult`
 * path with the human as requester and the investigation working-state CAS; the
 * response projects the persisted ledger row. `GET …/consultations?task_id=`
 * reads the same registry filtered on the task context. A consultation never
 * writes a task status, a task result, a proof or a decision, so its hooks only
 * invalidate read projections (ledger, timeline, working state).
 *
 * The idempotency key is owned by the caller, not by the mutation hook: the
 * panel keeps one key per frozen request body so a retry after a network
 * timeout replays the exact same operation instead of creating a new one.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { investigationQueryKeys } from "../../../api/investigation";
import {
  createIdempotencyKey,
  getJson,
  investigationPath,
  postJson,
  queryString,
} from "./client";
import { plansKeys, type InvestigationConsultationV1 } from "./plans";
import { tasksKeys } from "./tasks";
import type { CommonErrorCode, Uuid } from "./types";

export type TaskConsultationType =
  "interpretation" | "critique" | "pivot_strategy";

/** Route-level refusals plus the canonical invocation/advisor service codes. */
export type TaskConsultationErrorCode =
  | CommonErrorCode
  | "task_not_found"
  | "task_consultation_task_terminal"
  | "task_consultation_question_required"
  | "task_consultation_anchor_invalid"
  | "task_consultation_context_invalid"
  | "task_consultation_not_projected"
  | "stale_working_state"
  | "bridge_checkpoint_pending"
  | "external_disclosure_denied"
  | "operator_payload_invalid_or_sensitive"
  | "operator_mode_required"
  | "requester_actor_id_required"
  | "invocation_attribution_invalid"
  | "idempotency_key_invalid"
  | "idempotency_scope_invalid"
  | "investigation_not_started";

export interface TaskConsultationRequestV1 {
  consultation_type: TaskConsultationType;
  question: string;
  include_previous_consultation?: boolean;
  anchor?: string;
  expected_working_state_version: number;
  expected_working_state_sha256?: string;
}

/**
 * `InvestigationConsultationV1` plus the two additive fields migration 0028
 * exposes: the human task context of the request and the archived output hash.
 */
export interface TaskConsultationV1 extends InvestigationConsultationV1 {
  context_task_id: Uuid | null;
  output_sha256: string | null;
}

export interface TaskConsultationResponseV1 {
  invocation_id: Uuid;
  invocation_status: string;
  context_task_id: Uuid;
  task_context_sha256: string;
  consultation: TaskConsultationV1;
}

export interface TaskConsultationLedgerV1 {
  investigation_id: Uuid;
  consultations: TaskConsultationV1[];
}

/**
 * The six states the cockpit may display. They are derived from the persisted
 * registry row (invocation status, parse status, reconciliation state), never
 * from local optimism.
 */
export type TaskConsultationState =
  "sent" | "received" | "partial" | "refused" | "ambiguous";

export const TASK_CONSULTATION_STATE_LABELS: Record<
  TaskConsultationState,
  string
> = {
  sent: "Envoyée — en cours",
  received: "Reçue",
  partial: "Partielle",
  refused: "Refusée",
  ambiguous: "Ambiguë à réconcilier",
};

export const TASK_CONSULTATION_TYPE_LABELS: Record<
  TaskConsultationType,
  string
> = {
  interpretation: "Avis",
  critique: "Critique technique",
  pivot_strategy: "Pistes de pivot",
};

/** Registry rows may also carry plan/editorial types; label them as-is. */
export function taskConsultationTypeLabel(value: string): string {
  if (
    value === "interpretation" ||
    value === "critique" ||
    value === "pivot_strategy"
  ) {
    return TASK_CONSULTATION_TYPE_LABELS[value];
  }
  if (value === "plan_investigation") return "Révision de plan";
  if (value === "draft_text") return "Rédaction de Note";
  if (value === "review_text") return "Correction de Note";
  return value;
}

type StateInput = Pick<
  TaskConsultationV1,
  "invocation_status" | "parse_status" | "reconciliation_state"
>;

/**
 * A still-running invocation is "sent" first; otherwise ambiguity wins over
 * any terminal reading: `needs_review` or a `required` reconciliation means the
 * effect is not settled.
 */
export function deriveTaskConsultationState(
  consultation: StateInput,
): TaskConsultationState {
  if (
    consultation.invocation_status === "pending" ||
    consultation.invocation_status === "running"
  ) {
    return "sent";
  }
  if (
    consultation.invocation_status === "needs_review" ||
    consultation.reconciliation_state === "required"
  ) {
    return "ambiguous";
  }
  if (
    consultation.invocation_status === "failed" ||
    consultation.invocation_status === "policy_denied" ||
    consultation.invocation_status === "unavailable"
  ) {
    return "refused";
  }
  if (consultation.invocation_status === "succeeded") {
    return consultation.parse_status === "parsed" ? "received" : "partial";
  }
  if (
    consultation.invocation_status === "partial" ||
    consultation.invocation_status === "empty"
  ) {
    return "partial";
  }
  return "ambiguous";
}

export const taskConsultationsKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "task-consultations"] as const,
  list: (investigationId: string, taskId: string) =>
    [...taskConsultationsKeys.all(investigationId), taskId] as const,
};

export function listTaskConsultations(
  investigationId: string,
  taskId: string,
  signal?: AbortSignal,
): Promise<TaskConsultationLedgerV1> {
  return getJson<TaskConsultationLedgerV1>(
    investigationPath(investigationId, "/consultations") +
      queryString({ task_id: taskId }),
    signal,
  );
}

export function requestTaskConsultation(
  investigationId: string,
  taskId: string,
  input: TaskConsultationRequestV1,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<TaskConsultationResponseV1> {
  return postJson<TaskConsultationResponseV1>(
    investigationPath(
      investigationId,
      `/tasks/${encodeURIComponent(taskId)}/consultations`,
    ),
    input,
    idempotencyKey,
    signal,
  );
}

export function useTaskConsultations(
  investigationId: string,
  taskId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: taskConsultationsKeys.list(investigationId, taskId),
    queryFn: ({ signal }) =>
      listTaskConsultations(investigationId, taskId, signal),
    enabled: options.enabled ?? (investigationId !== "" && taskId !== ""),
  });
}

export interface TaskConsultationMutationVariables {
  input: TaskConsultationRequestV1;
  /** Supplied by the panel to freeze one key per request body. */
  idempotencyKey?: string;
}

/**
 * Invalidation: the route writes one invocation plus its advisor observations,
 * so the task ledger, the plan ledger, the timeline and the working-state
 * version change. Task projections deliberately stay untouched: a consultation
 * never mutates a task.
 */
export function useRequestTaskConsultation(
  investigationId: string,
  taskId: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      input,
      idempotencyKey,
    }: TaskConsultationMutationVariables) =>
      requestTaskConsultation(
        investigationId,
        taskId,
        input,
        idempotencyKey ?? createIdempotencyKey(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: taskConsultationsKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: plansKeys.consultations(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: investigationQueryKeys.detail(investigationId),
      });
    },
  });
}
