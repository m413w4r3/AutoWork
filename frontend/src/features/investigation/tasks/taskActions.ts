/**
 * Client-side mirror of the ADR-0011 D3 task permission matrix for a human.
 *
 * The cockpit must only render an action that is really executable. The
 * server's `TaskView` exposes no `allowed_*` field, so availability is derived
 * here from the status, the assignment and the action's own preconditions;
 * the server stays the final authority and still answers typed 403/409/422
 * refusals. Operator-only transitions (`start` with a lease, `heartbeat`,
 * `request_input`, `resume`) are deliberately absent: the browser sends human
 * commands only.
 *
 * "Demander un avis à Bridge" and "Demander une rédaction" are not listed:
 * the frozen API client exposes no task-scoped consultation or drafting route.
 */

import type { AgentSessionView } from "../api/agentSessions";
import type { TaskView } from "../api/tasks";
import type { AgentSessionState, TaskStatus } from "../api/types";

export type TaskActionId =
  | "assign_codex"
  | "assign_claude"
  | "take"
  | "transfer"
  | "resume_task"
  | "start"
  | "release"
  | "submit"
  | "examine_results"
  | "accept_result"
  | "request_changes"
  | "interrupt_session"
  | "resume_session"
  | "interrupt_task"
  | "add_note";

export const TASK_ACTION_LABELS: Record<TaskActionId, string> = {
  assign_codex: "Attribuer à Codex",
  assign_claude: "Attribuer à Claude Code",
  take: "Je prends cette tâche",
  transfer: "Transférer",
  resume_task: "Reprendre",
  start: "Démarrer la tâche",
  release: "Libérer la tâche",
  submit: "Soumettre le résultat",
  examine_results: "Examiner les résultats",
  accept_result: "Accepter le résultat",
  request_changes: "Demander des modifications",
  interrupt_session: "Interrompre la session",
  resume_session: "Reprendre la session",
  interrupt_task: "Interrompre",
  add_note: "Ajouter une note",
};

/** The "Interrompre" wording only fits a live task; earlier states cancel. */
export function taskActionLabel(action: TaskActionId, task: TaskView): string {
  if (action === "interrupt_task") {
    return task.status === "running" || task.status === "awaiting_input"
      ? "Interrompre"
      : "Annuler la tâche";
  }
  return TASK_ACTION_LABELS[action];
}

export const ACTIVE_SESSION_STATES: ReadonlySet<AgentSessionState> =
  new Set<AgentSessionState>([
    "provisioning",
    "ready",
    "running",
    "awaiting_instruction",
    "interrupt_requested",
    "reconciling",
  ]);

export const RESUMABLE_SESSION_STATES: ReadonlySet<AgentSessionState> =
  new Set<AgentSessionState>(["stopped", "lost"]);

const INTERRUPTIBLE_SESSION_STATES: ReadonlySet<AgentSessionState> =
  new Set<AgentSessionState>(["running", "awaiting_instruction"]);

const TERMINAL_TASK_STATUSES: ReadonlySet<TaskStatus> = new Set<TaskStatus>([
  "completed",
  "cancelled",
  "failed",
]);

const TRANSFERABLE_TASK_STATUSES: ReadonlySet<TaskStatus> = new Set<TaskStatus>(
  ["ready", "assigned", "running", "awaiting_input"],
);

/** Human `cancel` is admitted from every non-terminal state. */
const CANCELABLE_TASK_STATUSES: ReadonlySet<TaskStatus> = new Set<TaskStatus>([
  "proposed",
  "ready",
  "assigned",
  "running",
  "awaiting_input",
  "awaiting_review",
  "blocked",
]);

export interface TaskActionContext {
  /**
   * `null` means the workspace status is still unknown: actions that need the
   * host workspace are withheld until the server proves it available.
   */
  workspaceAvailable: boolean | null;
  sessions: readonly AgentSessionView[];
}

/**
 * The session an action may target: the newest active one, else the newest
 * resumable one (stopped/lost). Closed and failed sessions never qualify.
 */
export function sessionForTask(
  sessions: readonly AgentSessionView[],
  taskId: string,
): AgentSessionView | null {
  const matching = sessions.filter((session) => session.task_id === taskId);
  const byRecency = (left: AgentSessionView, right: AgentSessionView) =>
    right.updated_at.localeCompare(left.updated_at);
  const active = matching
    .filter((session) => ACTIVE_SESSION_STATES.has(session.state))
    .sort(byRecency)[0];
  if (active !== undefined) return active;
  const resumable = matching
    .filter((session) => RESUMABLE_SESSION_STATES.has(session.state))
    .sort(byRecency)[0];
  return resumable ?? null;
}

/**
 * Actions a human may run on this task, in display order.
 *
 * Transfer routes (assign Codex/Claude, take, transfer) sync the host
 * workspace, so they require a proven workspace. Blocked tasks are resumed
 * first: the transfer route refuses a blocked task, and a bare `assign`
 * without a handoff would leave the operator unable to open a session.
 */
export function taskActionAvailability(
  task: TaskView,
  context: TaskActionContext,
): TaskActionId[] {
  const actions: TaskActionId[] = [];
  if (TERMINAL_TASK_STATUSES.has(task.status)) {
    if (task.result_refs.length > 0) actions.push("examine_results");
    return actions;
  }
  const workspaceReady = context.workspaceAvailable === true;
  const session = sessionForTask(context.sessions, task.id);
  const transferable =
    workspaceReady && TRANSFERABLE_TASK_STATUSES.has(task.status);

  if (task.status === "ready" && workspaceReady) {
    actions.push("take", "assign_codex", "assign_claude");
  }
  if (transferable) actions.push("transfer");
  if (task.status === "blocked") actions.push("resume_task");
  if (task.status === "assigned" && task.assigned_actor_kind === "human") {
    actions.push("start");
  }
  if (task.status === "assigned") actions.push("release");
  if (task.status === "running" && task.assigned_actor_kind === "human") {
    actions.push("submit");
  }
  if (task.status === "awaiting_review") {
    actions.push("examine_results", "accept_result", "request_changes");
  } else if (task.result_refs.length > 0) {
    actions.push("examine_results");
  }
  if (session !== null && INTERRUPTIBLE_SESSION_STATES.has(session.state)) {
    actions.push("interrupt_session");
  }
  if (session !== null && RESUMABLE_SESSION_STATES.has(session.state)) {
    actions.push("resume_session");
  }
  if (CANCELABLE_TASK_STATUSES.has(task.status)) actions.push("interrupt_task");
  if (workspaceReady) actions.push("add_note");
  return actions;
}
