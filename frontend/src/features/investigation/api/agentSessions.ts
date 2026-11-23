/**
 * Operator session reads and the human actions of `api/agent_sessions.py`
 * (M24-05B1). Only the browser-safe half lives here.
 *
 * Exposed: list/get, the human-only journal summary (operations, budget,
 * journal-derived broker view, handoff freshness, instructions), the
 * read-only capability probe, open, enqueue a human instruction, interrupt,
 * resume.
 * The D7-HMAC-only routes (`reconcile`, `collect`, the `ack` instruction
 * action and `tool-calls`) are deliberately absent — their secret never
 * reaches the browser. `POST /agent-sessions` returns the public session
 * fields only; the one-time secret is delivered to the host agent.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { tasksKeys } from "./tasks";
import { handoffsKeys } from "./handoffs";
import type { HandoffActor, HandoffFreshnessDiff } from "./handoffs";
import {
  createIdempotencyKey,
  getJson,
  investigationPath,
  postEmpty,
  postJson,
  queryString,
} from "./client";
import type {
  AgentSessionState,
  BudgetDimension,
  CommonErrorCode,
  HandoffFreshnessState,
  InstructionState,
  IsoTimestamp,
  JsonObject,
  OperatorAdapterKind,
  ToolInvocationActorKind,
  ToolInvocationStatus,
  Uuid,
} from "./types";

export type AgentSessionErrorCode =
  | CommonErrorCode
  | "agent_session_invalid"
  | "agent_session_not_found"
  | "agent_session_actor_not_allowed"
  | "active_session_exists"
  | "agent_session_state_conflict"
  | "instruction_conflict"
  | "resume_unavailable"
  | "new_handoff_required"
  | "agentic_operator_unavailable"
  | "agentic_operator_contract_invalid"
  | "agent_session_start_failed"
  | "agent_session_external_llm_not_allowed"
  | "agent_session_network_isolation_unavailable"
  | "agent_session_secret_delivery_unavailable"
  | "agent_session_secret_delivery_failed"
  | "note_refs_unavailable"
  | "note_ref_unresolved"
  | "note_ref_invalid"
  | "idempotency_key_required"
  | "handoff_invalid"
  | "handoff_not_found"
  | "handoff_ref_unresolved"
  | "handoff_ref_forbidden"
  | "handoff_version_conflict"
  | "handoff_task_stale"
  | "task_not_found"
  | "task_actor_not_allowed"
  | "task_transition_not_allowed"
  | "stale_task_version"
  | "lease_expired"
  | "stale_fence"
  | "task_limit_exceeded"
  | "task_dependency_invalid"
  | "task_key_conflict"
  | "task_command_invalid";

export interface AgentSessionView {
  id: Uuid;
  investigation_id: Uuid;
  task_id: Uuid;
  adapter_kind: OperatorAdapterKind;
  state: AgentSessionState;
  state_version: number;
  host_session_ref: string | null;
  handoff_id: Uuid | null;
  capability_sha256: string;
  probed_at: IsoTimestamp;
  task_lease_id: Uuid | null;
  task_lease_fence: number | null;
  checkpoint_ref: string | null;
  checkpoint_sha256: string | null;
  reconciliation: JsonObject | null;
  reconciliation_sha256: string | null;
  last_activity_at: IsoTimestamp | null;
  opened_at: IsoTimestamp;
  closed_at: IsoTimestamp | null;
  close_reason: string | null;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
}

export interface OpenAgentSessionInput {
  task_id: Uuid;
  adapter_kind: OperatorAdapterKind;
  handoff_id: Uuid;
  executable: string;
  /** The Pydantic field is `str | None`; `null` means "canonical default". */
  model?: string | null;
}

export interface OpenAgentSessionResponse {
  session: AgentSessionView;
}

/**
 * `GET /api/investigations/agent-sessions/capabilities` payload
 * (`api/agent_sessions.py::agent_session_capabilities`). The route only
 * answers 200 after a real host probe, so `available` is always true there;
 * the unavailable cases are typed 5xx refusals, never an `available: false`.
 */
export interface AgentSessionConfinementView {
  profile: string;
  bwrap_ok: boolean;
  netns: boolean;
  network_mode: "isolated_egress" | null;
  loopback_isolated: boolean;
}

export interface AgentSessionCapabilitiesView {
  adapter_kind: OperatorAdapterKind;
  executable: string;
  available: boolean;
  version: string;
  capability_sha256: string;
  probed_at: IsoTimestamp;
  supports_resume: boolean;
  supports_structured_output: boolean;
  supports_non_interactive: boolean;
  modes: string[];
  permission_modes: string[];
  confinement: AgentSessionConfinementView | null;
}

/** Refusals of the read-only probe, in addition to `CommonErrorCode`. */
export type AgentSessionCapabilitiesErrorCode =
  | CommonErrorCode
  | "agent_session_invalid"
  | "agent_session_network_isolation_unavailable"
  | "agentic_operator_unavailable"
  | "agentic_operator_contract_invalid"
  | "workspace_agent_error"
  | "workspace_local_unavailable";

/** What the probe needs: one adapter and the executable the host resolves. */
export interface AgentSessionCapabilityTarget {
  adapterKind: OperatorAdapterKind;
  executable: string;
}

export interface AgentInstructionInput {
  text: string;
  note_refs?: string[];
}

export interface EnqueuedInstructionView {
  id: Uuid;
  investigation_id: Uuid;
  task_id: Uuid;
  session_id: Uuid;
  state: InstructionState;
  text_sha256: string;
  queued_at: IsoTimestamp;
  delivered_at: IsoTimestamp | null;
  acknowledged_at: IsoTimestamp | null;
  created_by: { kind: string; actor_id: string };
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
  queue_position: number;
  delivered_live: boolean;
  note_diffs: Record<string, string>[];
}

/**
 * `GET /api/investigations/{id}/agent-sessions/{sid}/summary`
 * (`application/agentic_sessions.py::SessionService.summary`, lot B6).
 *
 * A human-only, read-only projection of one session: the journal rows
 * attributed to it, their budget sums beside the canonical envelope, a
 * broker block *derived from* that journal, the session's handoff with its
 * freshness, and the latest human instructions. Authentication material
 * (secret, nonce, hash, delivery token) is never part of this payload.
 */
export interface AgentSessionOperationView {
  invocation_id: Uuid;
  tool_name: string;
  schema_version: string;
  status: ToolInvocationStatus;
  actor_kind: ToolInvocationActorKind | null;
  actor_id: string | null;
  /** Only the dimensions carrying a non-zero amount are present. */
  reserved_budget: Partial<Record<BudgetDimension, number>>;
  consumed_budget: Partial<Record<BudgetDimension, number>>;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
  version: number;
}

export interface AgentSessionOperationWindowView {
  items: AgentSessionOperationView[];
  total: number;
  limit: number;
  truncated: boolean;
}

export interface AgentSessionBudgetTotalsView {
  invocation_count: number;
  consumed: Record<BudgetDimension, number>;
  reserved: Record<BudgetDimension, number>;
}

/** `budget.envelope` is the canonical approved envelope; `null` fields mean
 * "dimension unbounded", never "unknown". */
export interface AgentSessionEnvelopeBudgetView {
  present: boolean;
  version: number | null;
  approved_at: IsoTimestamp | null;
  limits: Record<BudgetDimension, number | null> | null;
  consumed: Record<BudgetDimension, number> | null;
  reserved: Record<BudgetDimension, number> | null;
  remaining: Record<BudgetDimension, number | null> | null;
}

export interface AgentSessionBudgetView {
  session: AgentSessionBudgetTotalsView;
  envelope: AgentSessionEnvelopeBudgetView;
}

/** No live broker state is read: `derived_from` names the journal it sums. */
export interface AgentSessionBrokerView {
  derived_from: "tool_invocations_journal";
  last_signed_call: AgentSessionOperationView | null;
  signed_call_count: number;
  needs_review_count: number;
  reconciliation_required: boolean;
}

export type AgentSessionHandoffFreshnessView =
  | { checked: false; reason: string }
  | {
      checked: true;
      state: HandoffFreshnessState;
      diffs: HandoffFreshnessDiff[];
    };

export interface AgentSessionHandoffSummaryView {
  handoff_id: Uuid;
  version: number;
  sha256: string;
  created_at: IsoTimestamp;
  from_actor: HandoffActor;
  to_actor: HandoffActor;
  freshness: AgentSessionHandoffFreshnessView | null;
}

export interface AgentSessionInstructionView {
  instruction_id: Uuid;
  /** The journal carries the full domain vocabulary, including the
   * transient `delivering` / `delivery_unknown` states the enqueue route
   * never returns. */
  state: InstructionState;
  text: string;
  text_sha256: string;
  /** 0 for an instruction that is no longer waiting in the queue. */
  queue_position: number;
  created_by: { kind: string; actor_id: string };
  queued_at: IsoTimestamp;
  delivered_at: IsoTimestamp | null;
  acknowledged_at: IsoTimestamp | null;
  delivery_attempts: number;
  delivery_mode: "live" | "next_run" | null;
  delivery_sha256: string | null;
  last_delivery_error: string | null;
  cancelled_at: IsoTimestamp | null;
  cancel_reason: string | null;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
}

export interface AgentSessionInstructionWindowView {
  items: AgentSessionInstructionView[];
  total: number;
  limit: number;
  truncated: boolean;
}

export interface AgentSessionSummaryView {
  session_id: Uuid;
  operations: AgentSessionOperationWindowView;
  budget: AgentSessionBudgetView;
  broker: AgentSessionBrokerView;
  handoff: AgentSessionHandoffSummaryView | null;
  instructions: AgentSessionInstructionWindowView;
}

export const agentSessionsKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "agent-sessions"] as const,
  list: (investigationId: string, taskId?: string) =>
    [...agentSessionsKeys.all(investigationId), "list", taskId ?? ""] as const,
  detail: (investigationId: string, sessionId: string) =>
    [...agentSessionsKeys.all(investigationId), sessionId] as const,
  /**
   * Sits under the detail key, so invalidating one session's detail also
   * refreshes its journal-derived summary.
   */
  summary: (investigationId: string, sessionId: string) =>
    [
      ...agentSessionsKeys.detail(investigationId, sessionId),
      "summary",
    ] as const,
  /**
   * The probe is not investigation-scoped (`/api/investigations/agent-sessions/
   * capabilities`); its key deliberately sits outside every
   * `["investigation", <id>, …]` prefix.
   */
  capabilities: (adapterKind: OperatorAdapterKind, executable: string) =>
    ["agent-sessions", "capabilities", adapterKind, executable] as const,
};

export function listAgentSessions(
  investigationId: string,
  taskId?: string,
  signal?: AbortSignal,
): Promise<AgentSessionView[]> {
  return getJson<AgentSessionView[]>(
    investigationPath(investigationId, "/agent-sessions") +
      queryString({ task_id: taskId }),
    signal,
  );
}

export function getAgentSession(
  investigationId: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<AgentSessionView> {
  return getJson<AgentSessionView>(
    investigationPath(
      investigationId,
      `/agent-sessions/${encodeURIComponent(sessionId)}`,
    ),
    signal,
  );
}

export function getAgentSessionSummary(
  investigationId: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<AgentSessionSummaryView> {
  return getJson<AgentSessionSummaryView>(
    investigationPath(
      investigationId,
      `/agent-sessions/${encodeURIComponent(sessionId)}/summary`,
    ),
    signal,
  );
}

/**
 * Read-only, human-gated probe of the host CLI. `executable` is the value the
 * human would pass to open a session; the response echoes it and nothing is
 * executed by the browser. A host that cannot prove its loopback isolation
 * answers a typed 503 instead of a capabilities payload.
 */
export function getAgentSessionCapabilities(
  adapterKind: OperatorAdapterKind,
  executable: string,
  signal?: AbortSignal,
): Promise<AgentSessionCapabilitiesView> {
  return getJson<AgentSessionCapabilitiesView>(
    "/api/investigations/agent-sessions/capabilities" +
      queryString({ adapter_kind: adapterKind, executable }),
    signal,
  );
}

export function openAgentSession(
  investigationId: string,
  input: OpenAgentSessionInput,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<OpenAgentSessionResponse> {
  return postJson<OpenAgentSessionResponse>(
    investigationPath(investigationId, "/agent-sessions"),
    input,
    idempotencyKey,
    signal,
  );
}

/** Human enqueue only; `action: "ack"` is the operator's signed action. */
export function enqueueAgentInstruction(
  investigationId: string,
  sessionId: string,
  input: AgentInstructionInput,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<EnqueuedInstructionView> {
  return postJson<EnqueuedInstructionView>(
    investigationPath(
      investigationId,
      `/agent-sessions/${encodeURIComponent(sessionId)}/instructions`,
    ),
    {
      action: "enqueue",
      text: input.text,
      note_refs: input.note_refs ?? [],
    },
    idempotencyKey,
    signal,
  );
}

export function interruptAgentSession(
  investigationId: string,
  sessionId: string,
  reason: string,
  signal?: AbortSignal,
): Promise<AgentSessionView> {
  return postJson<AgentSessionView>(
    investigationPath(
      investigationId,
      `/agent-sessions/${encodeURIComponent(sessionId)}/interrupt`,
    ),
    { reason },
    undefined,
    signal,
  );
}

export function resumeAgentSession(
  investigationId: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<AgentSessionView> {
  return postEmpty<AgentSessionView>(
    investigationPath(
      investigationId,
      `/agent-sessions/${encodeURIComponent(sessionId)}/resume`,
    ),
    undefined,
    signal,
  );
}

export function useAgentSessions(
  investigationId: string,
  taskId?: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: agentSessionsKeys.list(investigationId, taskId),
    queryFn: ({ signal }) => listAgentSessions(investigationId, taskId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useAgentSession(
  investigationId: string,
  sessionId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: agentSessionsKeys.detail(investigationId, sessionId),
    queryFn: ({ signal }) =>
      getAgentSession(investigationId, sessionId, signal),
    enabled: options.enabled ?? (investigationId !== "" && sessionId !== ""),
  });
}

/** Human-only journal projection of one session; no polling contract. */
export function useAgentSessionSummary(
  investigationId: string,
  sessionId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: agentSessionsKeys.summary(investigationId, sessionId),
    queryFn: ({ signal }) =>
      getAgentSessionSummary(investigationId, sessionId, signal),
    enabled: options.enabled ?? (investigationId !== "" && sessionId !== ""),
  });
}

/**
 * Real capability probe of one adapter/executable pair. `target === null`
 * disables the query; the placeholder key never collides with an enabled one
 * because `enabled` is false. The probe is a live host read with no cache
 * contract, so callers treat every mount as a fresh observation.
 */
export function useAgentSessionCapabilities(
  target: AgentSessionCapabilityTarget | null,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: agentSessionsKeys.capabilities(
      target?.adapterKind ?? "codex_cli",
      target?.executable ?? "",
    ),
    queryFn: ({ signal }) => {
      if (target === null) {
        throw new Error("agent session capability target is required");
      }
      return getAgentSessionCapabilities(
        target.adapterKind,
        target.executable,
        signal,
      );
    },
    enabled: options.enabled ?? target !== null,
  });
}

/**
 * Invalidation: opening a session consumes a handoff, moves the task to
 * `running` and appends `session_started`; sessions, tasks and handoffs (the
 * consumed version) are stale.
 */
export function useOpenAgentSession(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: OpenAgentSessionInput) =>
      openAgentSession(investigationId, input, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: agentSessionsKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: handoffsKeys.all(investigationId),
      });
    },
  });
}

/** Invalidation: the instruction queue view of this session changes. */
export function useEnqueueAgentInstruction(
  investigationId: string,
  sessionId: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: AgentInstructionInput) =>
      enqueueAgentInstruction(
        investigationId,
        sessionId,
        input,
        createIdempotencyKey(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: agentSessionsKeys.detail(investigationId, sessionId),
      });
      void queryClient.invalidateQueries({
        queryKey: agentSessionsKeys.summary(investigationId, sessionId),
      });
      void queryClient.invalidateQueries({
        queryKey: agentSessionsKeys.list(investigationId),
      });
    },
  });
}

/**
 * Invalidation: interrupt/resume change the session state and may close the
 * task lease, so the session, task and timeline projections are stale.
 */
function useSessionLifecycleMutation<TVariables>(
  investigationId: string,
  mutationFn: (variables: TVariables) => Promise<AgentSessionView>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: agentSessionsKeys.all(investigationId),
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

export function useInterruptAgentSession(
  investigationId: string,
  sessionId: string,
) {
  return useSessionLifecycleMutation(investigationId, (reason: string) =>
    interruptAgentSession(investigationId, sessionId, reason),
  );
}

export function useResumeAgentSession(
  investigationId: string,
  sessionId: string,
) {
  return useSessionLifecycleMutation(investigationId, () =>
    resumeAgentSession(investigationId, sessionId),
  );
}
