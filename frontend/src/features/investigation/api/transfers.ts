/**
 * Human transfer of one task (`api/investigation_transfers.py`, M24-X1).
 *
 * One auditable flow: sync the host workspace, reconcile/reassign through the
 * task engine, append a new immutable handoff version, and optionally open the
 * destination operator session. The route is human-gated, carries
 * `Idempotency-Key` and a CAS `expected_status_version`; Bridge is never a
 * destination (it is reached through `advisor.consult`).
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { agentSessionsKeys } from "./agentSessions";
import { handoffsKeys } from "./handoffs";
import { tasksKeys } from "./tasks";
import { workspaceKeys } from "./workspace";
import { createIdempotencyKey, investigationPath, postJson } from "./client";
import type { AgentSessionView } from "./agentSessions";
import type {
  HandoffFreshnessReport,
  HandoffPartyKind,
  InvestigationHandoffV1,
} from "./handoffs";
import type {
  CommonErrorCode,
  HandoffActorKind,
  IsoTimestamp,
  OperatorAdapterKind,
  ResultVerificationStatus,
  TaskStatus,
  Uuid,
} from "./types";

export type TransferErrorCode =
  | CommonErrorCode
  | "transfer_invalid"
  | "transfer_not_found"
  | "transfer_forbidden"
  | "transfer_status_not_allowed"
  | "stale_task_version"
  | "transfer_conflict"
  | "transfer_handoff_too_large"
  | "workspace_sync_incomplete"
  | "workspace_local_unavailable"
  | "workspace_not_found"
  | "workspace_identity_conflict"
  | "workspace_unmanaged_directory"
  | "revision_conflict"
  | "concurrent_change"
  | "policy_denied"
  | "busy"
  | "length_required"
  | "size_limit"
  | "hash_mismatch"
  | "cursor_expired"
  | "invalid_response"
  | "invalid_request"
  | "invalid_path"
  | "unauthorized"
  | "not_implemented"
  | "internal_error"
  | "handoff_invalid"
  | "handoff_not_found"
  | "handoff_ref_unresolved"
  | "handoff_ref_forbidden"
  | "handoff_version_conflict"
  | "handoff_task_stale"
  | "task_not_found"
  | "task_actor_not_allowed"
  | "task_transition_not_allowed"
  | "lease_expired"
  | "stale_fence"
  | "agent_session_invalid"
  | "agent_session_not_found"
  | "agent_session_actor_not_allowed"
  | "active_session_exists"
  | "agent_session_state_conflict"
  | "resume_unavailable"
  | "new_handoff_required"
  | "agentic_operator_unavailable"
  | "agent_session_start_failed"
  | "agent_session_secret_delivery_unavailable"
  | "agent_session_secret_delivery_failed"
  | "note_refs_unavailable";

interface TransferBase {
  instruction?: string;
  expected_status_version: number;
}

/**
 * The route validates the destination shape itself: a human names no adapter
 * and never opens a session; an operator names one of the two CLI adapters,
 * and `open_session: true` requires the probed executable.
 */
export type TransferTaskRequest = TransferBase &
  (
    | {
        to_actor_kind: "human";
        to_adapter_kind?: never;
        open_session?: false;
        executable?: never;
        model?: never;
      }
    | {
        to_actor_kind: "operator";
        to_adapter_kind: OperatorAdapterKind;
        open_session: true;
        executable: string;
        model?: string;
      }
    | {
        to_actor_kind: "operator";
        to_adapter_kind: OperatorAdapterKind;
        open_session?: false;
        executable?: string;
        model?: string;
      }
  );

export interface TransferTaskState {
  id: Uuid;
  investigation_id: Uuid;
  task_key: string;
  status: TaskStatus;
  status_version: number;
  lease_fence: number;
  assigned_actor_kind: HandoffActorKind | null;
  assigned_actor_id: string | null;
  result_verification_status: ResultVerificationStatus;
  updated_at: IsoTimestamp;
}

export interface TransferDestination {
  actor_kind: HandoffPartyKind;
  actor_id: string;
}

export interface TransferSyncResult {
  generation: number;
  cursor: number;
  inserted_events: number;
  existing_events: number;
  inserted_revisions: number;
  resync_required: boolean;
  pages: number;
}

export interface TransferResult {
  task: TransferTaskState;
  destination: TransferDestination;
  sync: TransferSyncResult | null;
  handoff: InvestigationHandoffV1;
  previous_handoff_id: string | null;
  previous_handoff_version: number | null;
  previous_freshness: HandoffFreshnessReport | null;
  session: AgentSessionView | null;
  replayed: boolean;
}

export const transfersKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "transfers"] as const,
  mutation: (investigationId: string, taskId: string) =>
    [...transfersKeys.all(investigationId), taskId] as const,
};

export function transferTask(
  investigationId: string,
  taskId: string,
  input: TransferTaskRequest,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<TransferResult> {
  return postJson<TransferResult>(
    investigationPath(
      investigationId,
      `/tasks/${encodeURIComponent(taskId)}/transfer`,
    ),
    input,
    idempotencyKey,
    signal,
  );
}

/**
 * Invalidation: a transfer rewrites the task assignment/status, its lease and
 * session, appends a handoff version, may create an operator session and
 * re-syncs the host workspace. Every projection of those aggregates is stale.
 */
export function useTransferTask(investigationId: string, taskId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: transfersKeys.mutation(investigationId, taskId),
    mutationFn: (input: TransferTaskRequest) =>
      transferTask(investigationId, taskId, input, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: handoffsKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: agentSessionsKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: workspaceKeys.all(investigationId),
      });
    },
  });
}
