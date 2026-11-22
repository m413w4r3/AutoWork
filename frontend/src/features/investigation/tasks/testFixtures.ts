/**
 * Synthetic fixtures for the task and operator component tests. No live call,
 * no real identity: every value is a canonical-shaped placeholder.
 */

import type { AgentSessionView } from "../api/agentSessions";
import type { TaskView } from "../api/tasks";

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
