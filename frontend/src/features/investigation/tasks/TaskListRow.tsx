import type { KeyboardEvent } from "react";

import type { AgentSessionView } from "../api/agentSessions";
import type { TaskView } from "../api/tasks";
import {
  TASK_PRIORITY_LABELS,
  TASK_STATUS_LABELS,
  formatTimestamp,
  TASK_ACTOR_LABELS,
  actorRefLabel,
} from "./labels";

function requiresDecision(task: TaskView): boolean {
  return (
    task.status === "awaiting_review" ||
    (task.status === "blocked" && task.blocked_reason === "approval")
  );
}

function actorLabel(task: TaskView): string {
  if (task.assigned_actor_kind === null) return "Non attribuée";
  const kind = TASK_ACTOR_LABELS[task.assigned_actor_kind];
  if (task.assigned_actor_kind !== "operator") return kind;
  const actorId = task.assigned_actor_id ?? "";
  if (actorId.startsWith("operator:"))
    return `${kind} · ${actorRefLabel(actorId)}`;
  return kind;
}

export interface TaskListRowProps {
  task: TaskView;
  selected: boolean;
  sessions: readonly AgentSessionView[];
  optionId: string;
  onSelect: () => void;
  onShowResults: () => void;
  onArrow: (direction: -1 | 1) => void;
}

export function TaskListRow({
  task,
  selected,
  sessions,
  optionId,
  onSelect,
  onShowResults,
  onArrow,
}: TaskListRowProps) {
  const decisionRequired = requiresDecision(task);
  const session = sessions
    .filter((item) => item.task_id === task.id)
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))[0];
  const actor =
    session === undefined
      ? actorLabel(task)
      : `${actorLabel(task)} · ${session.adapter_kind === "codex_cli" ? "Codex CLI" : "Claude Code"}`;
  const hasResults =
    task.result_refs.length > 0 || task.status === "awaiting_review";

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      onArrow(event.key === "ArrowDown" ? 1 : -1);
    }
  }

  return (
    <li className="m24-task-row">
      <button
        id={optionId}
        type="button"
        className={
          selected
            ? "m24-task-row__select m24-task-row__select--selected"
            : "m24-task-row__select"
        }
        aria-pressed={selected}
        aria-label={`${task.title}, ${TASK_STATUS_LABELS[task.status]}, priorité ${TASK_PRIORITY_LABELS[task.priority]}, ${actor}${decisionRequired ? ", décision requise" : ""}`}
        onClick={onSelect}
        onKeyDown={handleKeyDown}
      >
        <span className="m24-task-row__badges">
          <span className={`m24-badge m24-badge--${task.status}`}>
            {TASK_STATUS_LABELS[task.status]}
          </span>
          <span className="m24-badge">
            {TASK_PRIORITY_LABELS[task.priority]}
          </span>
        </span>
        <strong className="m24-task-row__title">{task.title}</strong>
        <span className="m24-task-row__meta">
          <span>Acteur : {actor}</span>
          <span>Activité : {formatTimestamp(task.updated_at)}</span>
        </span>
        {decisionRequired ? (
          <span className="m24-badge m24-badge--awaiting_review">
            Décision requise
          </span>
        ) : null}
      </button>
      {hasResults ? (
        <button
          type="button"
          className="m24-task-row__results"
          aria-label={`Voir les résultats de ${task.title}`}
          onClick={onShowResults}
        >
          Résultats
        </button>
      ) : null}
    </li>
  );
}
