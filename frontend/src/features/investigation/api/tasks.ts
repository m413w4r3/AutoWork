/**
 * Task reads, the human task commands and the per-task/timeline event journals
 * (`api/investigation_tasks.py`, 04B3).
 *
 * The actor is resolved server-side. The browser only sends human commands;
 * an operator command is authenticated by the D7 HMAC session headers handled
 * by the host shim, never by this module. `create` and every command carry
 * `Idempotency-Key`; every command also carries `expected_status_version`
 * (CAS), and lease-bound commands carry the current `lease_fence`.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createIdempotencyKey,
  getJson,
  investigationPath,
  postJson,
  queryString,
} from "./client";
import type {
  BlockedReason,
  CommonErrorCode,
  IsoTimestamp,
  JsonObject,
  ResultVerificationStatus,
  TaskActorKind,
  TaskEventType,
  TaskPriority,
  TaskStatus,
  Uuid,
} from "./types";

export type TaskCommandName =
  | "assign"
  | "start"
  | "heartbeat"
  | "request_input"
  | "submit"
  | "accept_result"
  | "request_changes"
  | "block"
  | "unblock"
  | "resume"
  | "cancel"
  | "fail"
  | "release";

export type TaskErrorCode =
  | CommonErrorCode
  | "task_not_found"
  | "task_actor_not_allowed"
  | "stale_task_version"
  | "lease_expired"
  | "stale_fence"
  | "task_transition_not_allowed"
  | "agent_session_inactive"
  | "task_limit_exceeded"
  | "task_dependency_invalid"
  | "task_key_conflict"
  | "task_command_invalid"
  | "invalid_command_body"
  | "agent_session_mismatch"
  | "operator_task_scope_denied"
  | "invalid_event_cursor"
  | "invalid_timeline_cursor";

export interface TaskActorView {
  actor_kind: TaskActorKind;
  actor_id: string;
}

export interface TaskView {
  id: Uuid;
  investigation_id: Uuid;
  task_key: string;
  plan_version_id: Uuid | null;
  parent_task_id: Uuid | null;
  depth: number;
  title: string;
  question: string;
  objective: string;
  priority: TaskPriority;
  claim_refs: string[];
  evidence_refs: string[];
  sample_refs: string[];
  required_capabilities: string[];
  allowed_workspace_paths: string[];
  execution_policy: JsonObject;
  expected_outputs: string[];
  status: TaskStatus;
  status_version: number;
  result_verification_status: ResultVerificationStatus;
  blocked_reason: BlockedReason | null;
  assigned_actor_kind: TaskActorKind | null;
  assigned_actor_id: string | null;
  lease_id: Uuid | null;
  lease_session_id: Uuid | null;
  lease_fence: number;
  lease_expires_at: IsoTimestamp | null;
  heartbeat_at: IsoTimestamp | null;
  handoff_ref: Uuid | null;
  result_refs: JsonObject[];
  event_sequence: number;
  created_by: TaskActorView;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
}

export interface TaskEventView {
  id: Uuid;
  investigation_id: Uuid;
  task_id: Uuid;
  sequence: number;
  event_type: TaskEventType;
  from_status: TaskStatus | null;
  to_status: TaskStatus | null;
  status_version: number;
  lease_fence: number | null;
  actor: TaskActorView;
  idempotency_key: string;
  analyst_decision_id: Uuid | null;
  payload: JsonObject;
  occurred_at: IsoTimestamp;
}

export interface TaskCommandResponse {
  task: TaskView;
  event: TaskEventView | null;
  replayed: boolean;
}

export interface TaskPageView {
  items: TaskView[];
  total: number;
}

export interface TaskEventPageView {
  items: TaskEventView[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface TaskActorBody {
  actor_kind: "human" | "operator";
  actor_id: string;
}

export interface TaskCreateRequest {
  task_key: string;
  title: string;
  question?: string;
  objective?: string;
  priority?: TaskPriority;
  parent_task_id?: Uuid;
  plan_version_id?: Uuid;
  claim_refs?: string[];
  evidence_refs?: string[];
  sample_refs?: string[];
  required_capabilities?: string[];
  allowed_workspace_paths?: string[];
  execution_policy?: JsonObject;
  expected_outputs?: string[];
}

interface TaskCommandEnvelope {
  expected_status_version: number;
  target?: TaskActorBody;
  lease_fence?: number;
  reason?: string;
  blocked_reason?: BlockedReason;
  result_refs?: JsonObject[];
}

/**
 * The command shape required by `_run_command`: `assign` needs a target,
 * lease-bound commands need a fence, review/terminal decisions need a reason,
 * and `block` needs a typed blocked reason.
 */
export type TaskCommandRequest = TaskCommandEnvelope &
  (
    | { command: "assign"; target: TaskActorBody }
    | {
        command: "heartbeat" | "request_input" | "resume";
        lease_fence: number;
      }
    | {
        command: "accept_result" | "request_changes" | "cancel" | "fail";
        reason: string;
      }
    | { command: "block"; blocked_reason: BlockedReason }
    | { command: "start" | "release" | "unblock" | "submit" }
  );

export interface TaskPageParams {
  cursor?: string;
  limit?: number;
}

export interface TimelinePageParams {
  cursor?: string;
  limit?: number;
}

export type TimelineItemType =
  | "task_event"
  | "decision"
  | "observation"
  | "bridge_consultation"
  | "tool_invocation"
  | "plan_version";

export type TimelineActorKind = "human" | "operator" | "system" | "source";

export interface TimelineItemV1 {
  item_type: TimelineItemType;
  object_id: Uuid;
  occurred_at: IsoTimestamp;
  task_id: Uuid | null;
  actor_kind: TimelineActorKind;
  actor_id: string | null;
  summary: string;
  evidence: boolean;
  refs: Record<string, string>;
  linked_event_ids: Uuid[];
  linked_observation_ids: Uuid[];
}

export interface TimelinePageV1 {
  items: TimelineItemV1[];
  next_cursor: string | null;
  has_more: boolean;
}

export const tasksKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "tasks"] as const,
  list: (investigationId: string) =>
    [...tasksKeys.all(investigationId), "list"] as const,
  detail: (investigationId: string, taskId: string) =>
    [...tasksKeys.all(investigationId), taskId] as const,
  events: (
    investigationId: string,
    taskId: string,
    params: TaskPageParams = {},
  ) =>
    [
      ...tasksKeys.all(investigationId),
      taskId,
      "events",
      params.cursor ?? "",
      params.limit ?? null,
    ] as const,
  timeline: (investigationId: string, params: TimelinePageParams = {}) =>
    [
      "investigation",
      investigationId,
      "timeline",
      params.cursor ?? "",
      params.limit ?? null,
    ] as const,
};

export function listTasks(
  investigationId: string,
  signal?: AbortSignal,
): Promise<TaskPageView> {
  return getJson<TaskPageView>(
    investigationPath(investigationId, "/tasks"),
    signal,
  );
}

export function getTask(
  investigationId: string,
  taskId: string,
  signal?: AbortSignal,
): Promise<TaskView> {
  return getJson<TaskView>(
    investigationPath(investigationId, `/tasks/${encodeURIComponent(taskId)}`),
    signal,
  );
}

export function createTask(
  investigationId: string,
  input: TaskCreateRequest,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<TaskCommandResponse> {
  return postJson<TaskCommandResponse>(
    investigationPath(investigationId, "/tasks"),
    input,
    idempotencyKey,
    signal,
  );
}

export function runTaskCommand(
  investigationId: string,
  taskId: string,
  input: TaskCommandRequest,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<TaskCommandResponse> {
  return postJson<TaskCommandResponse>(
    investigationPath(
      investigationId,
      `/tasks/${encodeURIComponent(taskId)}/commands`,
    ),
    input,
    idempotencyKey,
    signal,
  );
}

export function listTaskEvents(
  investigationId: string,
  taskId: string,
  params: TaskPageParams = {},
  signal?: AbortSignal,
): Promise<TaskEventPageView> {
  return getJson<TaskEventPageView>(
    investigationPath(
      investigationId,
      `/tasks/${encodeURIComponent(taskId)}/events`,
    ) + queryString({ cursor: params.cursor, limit: params.limit }),
    signal,
  );
}

export function getTimeline(
  investigationId: string,
  params: TimelinePageParams = {},
  signal?: AbortSignal,
): Promise<TimelinePageV1> {
  return getJson<TimelinePageV1>(
    investigationPath(investigationId, "/timeline") +
      queryString({ cursor: params.cursor, limit: params.limit }),
    signal,
  );
}

export function useTasks(
  investigationId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: tasksKeys.list(investigationId),
    queryFn: ({ signal }) => listTasks(investigationId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useTask(
  investigationId: string,
  taskId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: tasksKeys.detail(investigationId, taskId),
    queryFn: ({ signal }) => getTask(investigationId, taskId, signal),
    enabled: options.enabled ?? (investigationId !== "" && taskId !== ""),
  });
}

export function useTaskEvents(
  investigationId: string,
  taskId: string,
  params: TaskPageParams = {},
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: tasksKeys.events(investigationId, taskId, params),
    queryFn: ({ signal }) =>
      listTaskEvents(investigationId, taskId, params, signal),
    enabled: options.enabled ?? (investigationId !== "" && taskId !== ""),
  });
}

export function useTimeline(
  investigationId: string,
  params: TimelinePageParams = {},
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: tasksKeys.timeline(investigationId, params),
    queryFn: ({ signal }) => getTimeline(investigationId, params, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

/**
 * Invalidation: a created task adds a row, a `task_created` event and a
 * timeline item; every task projection of the investigation is stale.
 */
export function useCreateTask(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: TaskCreateRequest) =>
      createTask(investigationId, input, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
    },
  });
}

/**
 * Invalidation: one command appends a task event, bumps the status version and
 * may create a decision, a lease or a contribution; the task detail, its event
 * page, the list and the timeline all change.
 */
export function useRunTaskCommand(investigationId: string, taskId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: TaskCommandRequest) =>
      runTaskCommand(investigationId, taskId, input, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.detail(investigationId, taskId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.list(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: [...tasksKeys.all(investigationId), taskId, "events"],
      });
    },
  });
}
