/**
 * Investigation plans, accepted-plan materialisation and the consultation
 * ledger (`api/investigation_plans.py`, 04A2).
 *
 * `POST /plans/request` reuses the canonical `advisor.consult` path with the
 * human as requester and a working-state CAS (`expected_version` /
 * `expected_sha256`). `POST /plans` only materialises an already persisted
 * advisor answer. `POST /plans/{id}/accept` is the human decision that
 * creates the tasks; it carries `Idempotency-Key` and optionally pins the
 * plan `expected_sha256`.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { tasksKeys } from "./tasks";
import {
  createIdempotencyKey,
  getJson,
  investigationPath,
  postJson,
} from "./client";
import type {
  CommonErrorCode,
  IsoTimestamp,
  ToolInvocationActorKind,
  ToolInvocationStatus,
  Uuid,
} from "./types";

export type PlanParseStatus = "parsed" | "partially_parsed" | "unparsed";
export type ConsultationParseStatus = PlanParseStatus;
export type ConsultationReconciliationState =
  "not_required" | "required" | "recorded" | "terminal";

/** Route-level refusals plus the canonical invocation/plan service codes. */
export type PlanErrorCode =
  | CommonErrorCode
  | "plan_not_found"
  | "plan_source_invocation_not_found"
  | "plan_source_invalid"
  | "plan_unparsed"
  | "stale_plan_version"
  | "plan_version_conflict"
  | "plan_materialization_conflict"
  | "plan_materialization_invalid"
  | "plan_dependency_unresolved"
  | "plan_dependency_cycle"
  | "investigation_plan_invalid"
  | "plan_provenance_invalid"
  | "plan_source_conflict"
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

export interface InvestigationLeadV1 {
  title: string;
  objective: string;
  cti_justification: string;
  claim_refs: string[];
  start_evidence_refs: string[];
  sample_refs: string[];
  tools: string[];
  preconditions: string[];
  interest: string;
  conclusion_criteria: string;
  limits: string;
  depends_on: string[];
  lead_id?: string;
}

export interface PlanContentV1 {
  schema_version: string;
  investigation_id: Uuid;
  version: number;
  leads: InvestigationLeadV1[];
}

export interface PlanVersionSummaryV1 {
  plan_version_id: Uuid;
  investigation_id: Uuid;
  version: number;
  schema_version: string;
  content_sha256: string;
  parse_status: PlanParseStatus;
  source_invocation_id: Uuid;
  source_observation_id: Uuid;
  supersedes_id: Uuid | null;
  created_at: IsoTimestamp;
  lead_count: number;
  current: boolean;
}

export interface PlanVersionDetailV1 extends PlanVersionSummaryV1 {
  content: PlanContentV1;
}

export interface PlanListV1 {
  current_version: number | null;
  plans: PlanVersionSummaryV1[];
}

export interface PlanRequestInput {
  expected_version: number;
  expected_sha256?: string;
  question?: string;
  /** Optional bounded task context (migration 0028); never evidence. */
  context_task_id?: Uuid;
}

export interface PlanRequestResponseV1 {
  investigation_id: Uuid;
  invocation_id: Uuid;
  invocation_status: string;
  idempotent_replay: boolean;
  advisor_parse_status: PlanParseStatus;
  plan_parse_status: PlanParseStatus;
  plan_version_id: Uuid | null;
  plan_version: number | null;
  missing_fields: string[];
  advice_only: boolean;
}

export interface PlanMaterializeInput {
  source_invocation_id: Uuid;
}

export interface PlanMaterializeResponseV1 {
  investigation_id: Uuid;
  plan_version_id: Uuid;
  plan_version: number;
  parse_status: PlanParseStatus;
  lead_count: number;
  created: boolean;
}

export interface PlanAcceptInput {
  expected_sha256?: string;
  reason?: string;
}

export interface PlanAcceptTaskV1 {
  task_id: Uuid;
  task_key: string;
  title: string;
  status: string;
  created: boolean;
}

export interface PlanAcceptResponseV1 {
  investigation_id: Uuid;
  plan_version_id: Uuid;
  plan_version: number;
  decision_id: Uuid;
  tasks: PlanAcceptTaskV1[];
  idempotent_replay: boolean;
}

export interface InvestigationConsultationV1 {
  invocation_id: Uuid;
  operation_key: string;
  consultation_type: string;
  invocation_status: ToolInvocationStatus;
  requester_actor_kind: ToolInvocationActorKind | null;
  requester_actor_id: string | null;
  task_id: Uuid | null;
  agent_session_id: Uuid | null;
  call_observation_id: Uuid | null;
  response_observation_id: Uuid | null;
  parse_status: ConsultationParseStatus;
  readable: boolean;
  usable: boolean;
  advisory_only: boolean;
  editorial_target: string | null;
  is_evidence: false;
  extracted_elements: string[];
  contributions: string[];
  limitations: string[];
  reconciliation_state: ConsultationReconciliationState;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
  response_observed_at: IsoTimestamp | null;
}

export interface ConsultationLedgerV1 {
  investigation_id: Uuid;
  consultations: InvestigationConsultationV1[];
}

export const plansKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "plans"] as const,
  list: (investigationId: string) =>
    [...plansKeys.all(investigationId), "list"] as const,
  detail: (investigationId: string, planVersionId: string) =>
    [...plansKeys.all(investigationId), planVersionId] as const,
  consultations: (investigationId: string) =>
    [...plansKeys.all(investigationId), "consultations"] as const,
};

export function listPlans(
  investigationId: string,
  signal?: AbortSignal,
): Promise<PlanListV1> {
  return getJson<PlanListV1>(
    investigationPath(investigationId, "/plans"),
    signal,
  );
}

export function getPlan(
  investigationId: string,
  planVersionId: string,
  signal?: AbortSignal,
): Promise<PlanVersionDetailV1> {
  return getJson<PlanVersionDetailV1>(
    investigationPath(
      investigationId,
      `/plans/${encodeURIComponent(planVersionId)}`,
    ),
    signal,
  );
}

export function listConsultations(
  investigationId: string,
  signal?: AbortSignal,
): Promise<ConsultationLedgerV1> {
  return getJson<ConsultationLedgerV1>(
    investigationPath(investigationId, "/consultations"),
    signal,
  );
}

export function requestPlan(
  investigationId: string,
  input: PlanRequestInput,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<PlanRequestResponseV1> {
  return postJson<PlanRequestResponseV1>(
    investigationPath(investigationId, "/plans/request"),
    input,
    idempotencyKey,
    signal,
  );
}

export function materializePlan(
  investigationId: string,
  input: PlanMaterializeInput,
  signal?: AbortSignal,
): Promise<PlanMaterializeResponseV1> {
  return postJson<PlanMaterializeResponseV1>(
    investigationPath(investigationId, "/plans"),
    input,
    undefined,
    signal,
  );
}

export function acceptPlan(
  investigationId: string,
  planVersionId: string,
  input: PlanAcceptInput,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<PlanAcceptResponseV1> {
  return postJson<PlanAcceptResponseV1>(
    investigationPath(
      investigationId,
      `/plans/${encodeURIComponent(planVersionId)}/accept`,
    ),
    input,
    idempotencyKey,
    signal,
  );
}

export function usePlans(
  investigationId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: plansKeys.list(investigationId),
    queryFn: ({ signal }) => listPlans(investigationId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function usePlan(
  investigationId: string,
  planVersionId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: plansKeys.detail(investigationId, planVersionId),
    queryFn: ({ signal }) => getPlan(investigationId, planVersionId, signal),
    enabled:
      options.enabled ?? (investigationId !== "" && planVersionId !== ""),
  });
}

export function useConsultationLedger(
  investigationId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: plansKeys.consultations(investigationId),
    queryFn: ({ signal }) => listConsultations(investigationId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export interface PlanRequestMutationVariables {
  input: PlanRequestInput;
  /** Supplied by the panel to freeze one key per request body. */
  idempotencyKey?: string;
}

/** Invalidation: a new plan version appends to the list and the ledger. */
export function useRequestPlan(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ input, idempotencyKey }: PlanRequestMutationVariables) =>
      requestPlan(
        investigationId,
        input,
        idempotencyKey ?? createIdempotencyKey(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: plansKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
    },
  });
}

/** Invalidation: materialisation appends a plan version and its ledger row. */
export function useMaterializePlan(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: PlanMaterializeInput) =>
      materializePlan(investigationId, input),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: plansKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
    },
  });
}

/**
 * Invalidation: accepting a plan writes the `plan_accept` decision and creates
 * tasks, so the plan list and every task projection (list, events, timeline)
 * are stale.
 */
export function useAcceptPlan(investigationId: string, planVersionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: PlanAcceptInput) =>
      acceptPlan(investigationId, planVersionId, input, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: plansKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
    },
  });
}
