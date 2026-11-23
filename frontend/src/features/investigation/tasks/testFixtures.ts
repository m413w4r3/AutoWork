/**
 * Synthetic fixtures for the task and operator component tests. No live call,
 * no real identity: every value is a canonical-shaped placeholder.
 */

import type {
  AgentSessionInstructionView,
  AgentSessionOperationView,
  AgentSessionSummaryView,
  AgentSessionView,
} from "../api/agentSessions";
import type { TaskView } from "../api/tasks";
import type { BudgetDimension } from "../api/types";

export const INVESTIGATION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
export const TASK_ID = "11111111-1111-4111-8111-111111111111";
export const SESSION_ID = "22222222-2222-4222-8222-222222222222";
export const HANDOFF_ID = "33333333-3333-4333-8333-333333333333";

export function makeTask(overrides: Partial<TaskView> = {}): TaskView {
  return {
    id: TASK_ID,
    investigation_id: INVESTIGATION_ID,
    task_key: "lead-1",
    plan_version_id: null,
    parent_task_id: null,
    depth: 0,
    title: "Vérifier le domaine C2",
    question: "Le domaine est-il un C2 ?",
    objective: "Confirmer le rôle du domaine dans la chaîne.",
    priority: "high",
    claim_refs: ["claim-1"],
    evidence_refs: [],
    sample_refs: [],
    required_capabilities: ["vt_read"],
    allowed_workspace_paths: ["results/"],
    execution_policy: {},
    expected_outputs: ["note"],
    status: "ready",
    status_version: 2,
    result_verification_status: "unverified",
    blocked_reason: null,
    assigned_actor_kind: null,
    assigned_actor_id: null,
    lease_id: null,
    lease_session_id: null,
    lease_fence: 0,
    lease_expires_at: null,
    heartbeat_at: null,
    handoff_ref: null,
    result_refs: [],
    event_sequence: 1,
    created_by: { actor_kind: "system", actor_id: "plan_accept:decision" },
    created_at: "2026-11-22T10:00:00Z",
    updated_at: "2026-11-22T10:00:00Z",
    ...overrides,
  };
}

export function makeSession(
  overrides: Partial<AgentSessionView> = {},
): AgentSessionView {
  return {
    id: SESSION_ID,
    investigation_id: INVESTIGATION_ID,
    task_id: TASK_ID,
    adapter_kind: "codex_cli",
    state: "running",
    state_version: 2,
    host_session_ref: "host-session-1",
    handoff_id: HANDOFF_ID,
    capability_sha256: "a".repeat(64),
    probed_at: "2026-11-22T09:00:00Z",
    task_lease_id: "44444444-4444-4444-8444-444444444444",
    task_lease_fence: 1,
    checkpoint_ref: "checkpoint-1",
    checkpoint_sha256: "b".repeat(64),
    reconciliation: null,
    reconciliation_sha256: null,
    last_activity_at: "2026-11-22T10:05:00Z",
    opened_at: "2026-11-22T09:00:00Z",
    closed_at: null,
    close_reason: null,
    created_at: "2026-11-22T09:00:00Z",
    updated_at: "2026-11-22T10:05:00Z",
    ...overrides,
  };
}

export function makeZeroBudget(): Record<BudgetDimension, number> {
  return {
    bridge_turns: 0,
    vt_units: 0,
    shodan_credits: 0,
    downloads: 0,
    tool_seconds: 0,
    context_tokens: 0,
    context_bytes: 0,
  };
}

export function makeSummaryOperation(
  overrides: Partial<AgentSessionOperationView> = {},
): AgentSessionOperationView {
  return {
    invocation_id: "66666666-6666-4666-8666-666666666666",
    tool_name: "vt.lookup",
    schema_version: "1",
    status: "succeeded",
    actor_kind: "operator",
    actor_id: "operator:codex_cli",
    reserved_budget: {},
    consumed_budget: {},
    created_at: "2026-11-22T10:01:00Z",
    updated_at: "2026-11-22T10:01:00Z",
    version: 1,
    ...overrides,
  };
}

export function makeSummaryInstruction(
  overrides: Partial<AgentSessionInstructionView> = {},
): AgentSessionInstructionView {
  return {
    instruction_id: "77777777-7777-4777-8777-777777777777",
    state: "acknowledged",
    text: "Priorise le PCAP.",
    text_sha256: "e".repeat(64),
    queue_position: 0,
    created_by: { kind: "human", actor_id: "analyst:test" },
    queued_at: "2026-11-22T10:02:00Z",
    delivered_at: "2026-11-22T10:02:10Z",
    acknowledged_at: "2026-11-22T10:02:20Z",
    delivery_attempts: 1,
    delivery_mode: "live",
    delivery_sha256: "f".repeat(64),
    last_delivery_error: null,
    cancelled_at: null,
    cancel_reason: null,
    created_at: "2026-11-22T10:02:00Z",
    updated_at: "2026-11-22T10:02:20Z",
    ...overrides,
  };
}

export function makeSummary(
  overrides: Partial<AgentSessionSummaryView> = {},
): AgentSessionSummaryView {
  return {
    session_id: SESSION_ID,
    operations: { items: [], total: 0, limit: 100, truncated: false },
    budget: {
      session: {
        invocation_count: 0,
        consumed: makeZeroBudget(),
        reserved: makeZeroBudget(),
      },
      envelope: {
        present: false,
        version: null,
        approved_at: null,
        limits: null,
        consumed: null,
        reserved: null,
        remaining: null,
      },
    },
    broker: {
      derived_from: "tool_invocations_journal",
      last_signed_call: null,
      signed_call_count: 0,
      needs_review_count: 0,
      reconciliation_required: false,
    },
    handoff: null,
    instructions: { items: [], total: 0, limit: 20, truncated: false },
    ...overrides,
  };
}
