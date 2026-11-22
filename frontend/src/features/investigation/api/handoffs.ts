/**
 * Immutable investigation handoffs (`api/investigation_handoffs.py`, 05A/D5).
 *
 * A human creates a handoff by citing canonical object ids: the service
 * resolves every id, derives the hashes/versions and refuses anything outside
 * the investigation. `POST /handoffs` carries `Idempotency-Key`. The same
 * route is also reachable by a D7-signed owning operator from the host shim;
 * the cockpit only sends the human identity.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { tasksKeys } from "./tasks";
import {
  createIdempotencyKey,
  getJson,
  investigationPath,
  postJson,
  queryString,
} from "./client";
import type {
  CommonErrorCode,
  HandoffFreshnessKind,
  HandoffFreshnessState,
  HandoffRefKind,
  IsoTimestamp,
  TaskStatus,
  Uuid,
} from "./types";

export type HandoffErrorCode =
  | CommonErrorCode
  | "handoff_invalid"
  | "handoff_not_found"
  | "handoff_ref_unresolved"
  | "handoff_ref_forbidden"
  | "handoff_forbidden"
  | "handoff_version_conflict"
  | "handoff_task_stale"
  | "agent_session_mismatch";

/**
 * Handoff parties are human or operator; the domain also has `system` for
 * `created_by`, but a handoff validator refuses it as a from/to actor.
 */
export type HandoffPartyKind = "human" | "operator";

export interface HandoffActor {
  kind: HandoffPartyKind;
  actor_id: string;
}

export interface HandoffTaskSnapshot {
  status: TaskStatus;
  status_version: number;
  question: string;
  objective: string;
  expected_outputs: string[];
  assigned_actor_kind: HandoffPartyKind | null;
  assigned_actor_id: string | null;
}

export interface HandoffPlanRef {
  plan_version_id: string;
  plan_version: number;
  plan_sha256: string;
}

export interface HandoffRef {
  kind: HandoffRefKind;
  id: string;
}

export interface HandoffWorkItem {
  text: string;
  refs: HandoffRef[];
}

export interface HandoffWork {
  hypotheses: HandoffWorkItem[];
  attempts: HandoffWorkItem[];
  established_results: HandoffWorkItem[];
  partial_results: HandoffWorkItem[];
  negative_results: HandoffWorkItem[];
  limits: HandoffWorkItem[];
}

export interface HandoffObservationRef {
  id: string;
  sha256: string;
}

export interface HandoffFindingRef {
  version_id: string;
  version: number;
}

export interface HandoffDecisionRef {
  analyst_decision_id: string;
}

export interface HandoffConsultationRef {
  invocation_id: string;
  consultation_type: string;
  output_sha256: string;
}

export interface HandoffRefs {
  observations: HandoffObservationRef[];
  findings: HandoffFindingRef[];
  decisions: HandoffDecisionRef[];
  consultations: HandoffConsultationRef[];
}

export interface HandoffFileRef {
  workspace_revision_id: string;
  file_relpath: string;
  file_sha256: string;
  observed_at: IsoTimestamp;
}

export interface HandoffFreshnessRef {
  kind: HandoffFreshnessKind;
  id: string;
  sha256: string | null;
  version: number | null;
}

export interface InvestigationHandoffV1 {
  schema_version: "investigation-handoff.v1";
  handoff_id: string;
  investigation_id: string;
  task_id: string;
  version: number;
  created_at: IsoTimestamp;
  from_actor: HandoffActor;
  to_actor: HandoffActor;
  task: HandoffTaskSnapshot;
  plan: HandoffPlanRef | null;
  work: HandoffWork;
  refs: HandoffRefs;
  files: HandoffFileRef[];
  next_action: string;
  freshness_refs: HandoffFreshnessRef[];
}

export interface HandoffCreateRequest {
  task_id: Uuid;
  to_actor: HandoffActor;
  work?: Partial<HandoffWork>;
  observation_ids?: Uuid[];
  finding_version_ids?: Uuid[];
  decision_ids?: Uuid[];
  consultation_invocation_ids?: Uuid[];
  file_revision_ids?: string[];
  next_action: string;
}

export interface HandoffFreshnessDiff {
  kind: HandoffFreshnessKind;
  id: string;
  recorded: string;
  current: string | null;
}

export interface HandoffFreshnessReport {
  handoff_id: string;
  state: HandoffFreshnessState;
  diffs: HandoffFreshnessDiff[];
}

export const handoffsKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "handoffs"] as const,
  list: (investigationId: string, taskId?: string) =>
    [...handoffsKeys.all(investigationId), "list", taskId ?? ""] as const,
  detail: (investigationId: string, handoffId: string) =>
    [...handoffsKeys.all(investigationId), handoffId] as const,
  freshness: (investigationId: string, handoffId: string) =>
    [...handoffsKeys.all(investigationId), handoffId, "freshness"] as const,
};

export function listHandoffs(
  investigationId: string,
  taskId?: string,
  signal?: AbortSignal,
): Promise<InvestigationHandoffV1[]> {
  return getJson<InvestigationHandoffV1[]>(
    investigationPath(investigationId, "/handoffs") +
      queryString({ task_id: taskId }),
    signal,
  );
}

export function createHandoff(
  investigationId: string,
  input: HandoffCreateRequest,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<InvestigationHandoffV1> {
  return postJson<InvestigationHandoffV1>(
    investigationPath(investigationId, "/handoffs"),
    input,
    idempotencyKey,
    signal,
  );
}

export function getHandoff(
  investigationId: string,
  handoffId: string,
  signal?: AbortSignal,
): Promise<InvestigationHandoffV1> {
  return getJson<InvestigationHandoffV1>(
    investigationPath(
      investigationId,
      `/handoffs/${encodeURIComponent(handoffId)}`,
    ),
    signal,
  );
}

export function getHandoffFreshness(
  investigationId: string,
  handoffId: string,
  signal?: AbortSignal,
): Promise<HandoffFreshnessReport> {
  return getJson<HandoffFreshnessReport>(
    investigationPath(
      investigationId,
      `/handoffs/${encodeURIComponent(handoffId)}/freshness`,
    ),
    signal,
  );
}

export function useHandoffs(
  investigationId: string,
  taskId?: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: handoffsKeys.list(investigationId, taskId),
    queryFn: ({ signal }) => listHandoffs(investigationId, taskId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useHandoff(
  investigationId: string,
  handoffId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: handoffsKeys.detail(investigationId, handoffId),
    queryFn: ({ signal }) => getHandoff(investigationId, handoffId, signal),
    enabled: options.enabled ?? (investigationId !== "" && handoffId !== ""),
  });
}

export function useHandoffFreshness(
  investigationId: string,
  handoffId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: handoffsKeys.freshness(investigationId, handoffId),
    queryFn: ({ signal }) =>
      getHandoffFreshness(investigationId, handoffId, signal),
    enabled: options.enabled ?? (investigationId !== "" && handoffId !== ""),
  });
}

/**
 * Invalidation: a handoff appends an immutable version, a `handoff_created`
 * task event and a timeline item; the task projection changes as well.
 */
export function useCreateHandoff(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: HandoffCreateRequest) =>
      createHandoff(investigationId, input, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: handoffsKeys.all(investigationId),
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
