/**
 * Filterable task list and one selected task detail for the M24 cockpit.
 * Server state stays in TanStack Query; selection and filters are local, with
 * the selected task mirrored to the existing workshop URL search params.
 */

import { useCallback, useEffect, useId, useMemo, useState } from "react";

import { useAgentSessions } from "../api/agentSessions";
import { toWorkshopApiError } from "../api/client";
import { useTasks } from "../api/tasks";
import type { TaskPriority, TaskStatus } from "../api/types";
import { useWorkspaceStatus } from "../api/workspace";
import { AVAILABILITY_LABELS } from "../workspace/workspaceLabels";
import { TaskCard, type TaskCardSubview } from "./TaskCard";
import { TaskListRow } from "./TaskListRow";
import { TASK_PRIORITY_LABELS, TASK_STATUS_LABELS } from "./labels";
import { ACTIVE_SESSION_STATES } from "./taskActions";

const STATUS_OPTIONS = Object.entries(TASK_STATUS_LABELS) as Array<
  [TaskStatus, string]
>;
const PRIORITY_OPTIONS = Object.entries(TASK_PRIORITY_LABELS) as Array<
  [TaskPriority, string]
>;
const PRIORITY_RANK: Record<TaskPriority, number> = {
  critical: 0,
  high: 1,
  normal: 2,
  low: 3,
};
const TASK_PARAM = "task";

type ActorFilter = "all" | "human" | "operator" | "unassigned";

function decisionRequired(status: TaskStatus, blockedReason: string | null) {
  return (
    status === "awaiting_review" ||
    (status === "blocked" && blockedReason === "approval")
  );
}

function workflowRank(task: {
  status: TaskStatus;
  blocked_reason: string | null;
}): number {
  if (decisionRequired(task.status, task.blocked_reason)) return 0;
  if (task.status === "running" || task.status === "awaiting_input") return 1;
  if (
    task.status === "ready" ||
    task.status === "assigned" ||
    task.status === "proposed"
  )
    return 2;
  if (task.status === "blocked") return 3;
  return 4;
}

function sortTasks<
  T extends {
    status: TaskStatus;
    blocked_reason: string | null;
    priority: TaskPriority;
    updated_at: string;
  },
>(tasks: readonly T[]): T[] {
  return [...tasks].sort(
    (left, right) =>
      workflowRank(left) - workflowRank(right) ||
      PRIORITY_RANK[left.priority] - PRIORITY_RANK[right.priority] ||
      right.updated_at.localeCompare(left.updated_at),
  );
}

function readTaskParam(): string | null {
  return new URL(window.location.href).searchParams.get(TASK_PARAM);
}

function writeTaskParam(taskId: string, replace = false) {
  const url = new URL(window.location.href);
  url.searchParams.set(TASK_PARAM, taskId);
  const method = replace ? "replaceState" : "pushState";
  const state = window.history.state as unknown;
  window.history[method](
    {
      ...(typeof state === "object" && state !== null ? state : {}),
      investigationTask: taskId,
    },
    "",
    url,
  );
}

function clearTaskParam() {
  const url = new URL(window.location.href);
  url.searchParams.delete(TASK_PARAM);
  window.history.replaceState(window.history.state, "", url);
}

function rowButtonId(prefix: string, index: number): string {
  return `${prefix}-task-row-${index}`;
}

export function TaskWorkspace({
  investigationId,
}: {
  investigationId: string;
}) {
  const rowIdPrefix = useId();
  const tasks = useTasks(investigationId);
  const sessions = useAgentSessions(investigationId);
  const workspace = useWorkspaceStatus(investigationId);
  const [status, setStatus] = useState<TaskStatus | "all">("all");
  const [priority, setPriority] = useState<TaskPriority | "all">("all");
  const [actor, setActor] = useState<ActorFilter>("all");
  const [text, setText] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(
    readTaskParam,
  );
  const [selectedSubview, setSelectedSubview] =
    useState<TaskCardSubview>("summary");
  const [selectionMessage, setSelectionMessage] = useState<string | null>(null);

  const items = useMemo(() => tasks.data?.items ?? [], [tasks.data]);
  const orderedItems = useMemo(() => sortTasks(items), [items]);
  const visible = useMemo(() => {
    const needle = text.trim().toLocaleLowerCase("fr");
    return orderedItems
      .filter((task) => status === "all" || task.status === status)
      .filter((task) => priority === "all" || task.priority === priority)
      .filter((task) => {
        if (actor === "all") return true;
        if (actor === "unassigned") return task.assigned_actor_kind === null;
        return task.assigned_actor_kind === actor;
      })
      .filter((task) => {
        if (needle === "") return true;
        return [task.title, task.question, task.objective, task.task_key].some(
          (value) => value.toLocaleLowerCase("fr").includes(needle),
        );
      });
  }, [orderedItems, status, priority, actor, text]);

  const operatorSessions = sessions.data ?? [];
  const workspaceAvailable =
    workspace.data === undefined ? null : workspace.data.workspace_available;
  const decisionTasks = orderedItems.filter((task) =>
    decisionRequired(task.status, task.blocked_reason),
  );
  const activeSessions = operatorSessions
    .filter((session) => ACTIVE_SESSION_STATES.has(session.state))
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
  const activeSessionTaskIds = [
    ...new Set(activeSessions.map((session) => session.task_id)),
  ];

  const selectTask = useCallback(
    (taskId: string, subview: TaskCardSubview = "summary") => {
      if (taskId !== selectedTaskId) writeTaskParam(taskId);
      setSelectedTaskId(taskId);
      setSelectedSubview(subview);
      setSelectionMessage(null);
    },
    [selectedTaskId],
  );

  // Selection follows browser Back/Forward. `vue` and all unrelated query
  // parameters are left untouched by this view.
  useEffect(() => {
    function handlePopState() {
      setSelectedTaskId(readTaskParam());
      setSelectedSubview("summary");
      setSelectionMessage(null);
    }
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  // Restore a valid URL task, choose a default task when none is selected, or
  // explain and discard an unknown id without exposing that id in the UI.
  useEffect(() => {
    if (!tasks.isSuccess) return;
    if (selectedTaskId === null) {
      const first = orderedItems[0];
      if (first !== undefined) setSelectedTaskId(first.id);
      return;
    }
    if (!items.some((task) => task.id === selectedTaskId)) {
      setSelectionMessage(
        "La tâche indiquée dans l’URL est introuvable. Une tâche par défaut a été sélectionnée.",
      );
      const fallback = orderedItems[0];
      setSelectedTaskId(fallback?.id ?? null);
      setSelectedSubview("summary");
      clearTaskParam();
      return;
    }
    if (
      visible.length > 0 &&
      !visible.some((task) => task.id === selectedTaskId)
    ) {
      const fallback = visible[0];
      if (fallback !== undefined) selectTask(fallback.id);
    }
  }, [
    items,
    orderedItems,
    selectedTaskId,
    selectTask,
    tasks.isSuccess,
    visible,
  ]);

  const selectedTask = items.find((task) => task.id === selectedTaskId) ?? null;
  const workspaceMessage = workspace.isPending
    ? "Vérification du workspace local…"
    : workspace.isError
      ? "Statut du workspace indisponible : les actions qui écrivent dans le workspace sont masquées."
      : workspaceAvailable === true
        ? "Workspace local disponible."
        : `Workspace local indisponible (${workspace.data === undefined ? "indisponible" : AVAILABILITY_LABELS[workspace.data.availability_code]}) : les attributions, transferts et notes sont masqués.`;

  function showDecisions() {
    const first = decisionTasks[0];
    if (first === undefined) return;
    setStatus("all");
    setPriority("all");
    setActor("all");
    setText("");
    selectTask(first.id, "results");
  }

  function showActiveSessions() {
    const firstSession = activeSessions.find((session) =>
      items.some((task) => task.id === session.task_id),
    );
    if (firstSession === undefined) return;
    setStatus("all");
    setPriority("all");
    setActor("all");
    setText("");
    selectTask(firstSession.task_id, "agent");
  }

  function handleArrow(index: number, direction: -1 | 1) {
    const nextIndex = Math.max(
      0,
      Math.min(visible.length - 1, index + direction),
    );
    const next = visible[nextIndex];
    if (next === undefined) return;
    selectTask(next.id);
    document.getElementById(rowButtonId(rowIdPrefix, nextIndex))?.focus();
  }

  return (
    <section
      className="m24-task-workspace"
      aria-label="Poste de travail des tâches"
    >
      <nav className="m24-task-shortcuts" aria-label="Raccourcis des tâches">
        {decisionTasks.length === 0 ? null : (
          <button
            type="button"
            className="m24-action m24-action--quiet"
            onClick={showDecisions}
          >
            Nouvelles décisions ({decisionTasks.length})
          </button>
        )}
        {activeSessionTaskIds.length === 0 ? null : (
          <button
            type="button"
            className="m24-action m24-action--quiet"
            onClick={showActiveSessions}
          >
            Sessions actives ({activeSessions.length})
          </button>
        )}
      </nav>
      <div className="m24-task-workspace__columns">
        <section
          className="m24-task-list-panel m24-card"
          aria-labelledby="m24-tasks-heading"
        >
          <div className="m24-task-list-panel__heading">
            <h3 id="m24-tasks-heading">Tâches</h3>
            <span className="m24-muted">
              {visible.length} / {items.length}
            </span>
          </div>
          <p className="m24-muted">
            Les dépendances déclarées des pistes restent dans le plan ; le
            contrat API n’expose pas de graphe de tâches.
          </p>
          {workspace.isError || workspaceAvailable !== true ? (
            <p
              className="m24-warning"
              role={workspace.isError ? "alert" : "status"}
            >
              {workspaceMessage}
            </p>
          ) : null}

          <div
            className="m24-filters m24-task-filters"
            role="group"
            aria-label="Filtres des tâches"
          >
            <label>
              Statut
              <select
                value={status}
                onChange={(event) =>
                  setStatus(event.target.value as TaskStatus | "all")
                }
              >
                <option value="all">Tous</option>
                {STATUS_OPTIONS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Priorité
              <select
                value={priority}
                onChange={(event) =>
                  setPriority(event.target.value as TaskPriority | "all")
                }
              >
                <option value="all">Toutes</option>
                {PRIORITY_OPTIONS.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Assignation
              <select
                value={actor}
                onChange={(event) =>
                  setActor(event.target.value as ActorFilter)
                }
              >
                <option value="all">Toutes</option>
                <option value="human">Humain</option>
                <option value="operator">Opérateur</option>
                <option value="unassigned">Non attribuée</option>
              </select>
            </label>
            <label>
              Recherche
              <input
                type="search"
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder="Titre, question, clé…"
              />
            </label>
          </div>

          <p role="status" aria-live="polite" className="m24-muted">
            {visible.length} tâche(s) affichée(s) sur {items.length}.
          </p>
          {tasks.isPending ? (
            <p role="status">Chargement des tâches…</p>
          ) : tasks.isError ? (
            <p role="alert" className="error-message">
              Les tâches ne peuvent pas être chargées :{" "}
              {toWorkshopApiError(tasks.error).code}.
            </p>
          ) : items.length === 0 ? (
            <p className="empty-state">
              Aucune tâche. Demandez un plan à Bridge puis acceptez-le pour
              matérialiser ses pistes.
            </p>
          ) : visible.length === 0 ? (
            <p className="empty-state">
              Aucune tâche ne correspond aux filtres.
            </p>
          ) : (
            <ul className="m24-task-list" aria-label="Liste des tâches">
              {visible.map((task, index) => (
                <TaskListRow
                  key={task.id}
                  task={task}
                  selected={task.id === selectedTaskId}
                  sessions={operatorSessions}
                  optionId={rowButtonId(rowIdPrefix, index)}
                  onSelect={() => selectTask(task.id)}
                  onShowResults={() => selectTask(task.id, "results")}
                  onArrow={(direction) => handleArrow(index, direction)}
                />
              ))}
            </ul>
          )}
        </section>

        <section
          className="m24-task-detail-panel"
          aria-label="Détail de la tâche sélectionnée"
        >
          {selectionMessage === null ? null : (
            <p role="status" className="m24-warning">
              {selectionMessage}
            </p>
          )}
          {selectedTask === null ? (
            <div className="m24-card">
              <p className="empty-state">
                Sélectionnez une tâche pour afficher son détail.
              </p>
            </div>
          ) : (
            <TaskCard
              key={selectedTask.id}
              investigationId={investigationId}
              task={selectedTask}
              allTasks={items}
              sessions={operatorSessions}
              workspaceAvailable={workspaceAvailable}
              activeSubview={selectedSubview}
              onSubviewChange={setSelectedSubview}
              onTaskSelect={(taskId) => selectTask(taskId)}
            />
          )}
        </section>
      </div>
    </section>
  );
}
