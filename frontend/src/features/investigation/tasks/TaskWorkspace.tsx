/**
 * Filterable task list of the M24 cockpit (desktop only).
 *
 * The server page is fetched once and filtered client-side: the frozen task
 * API exposes no server-side filter parameters. Dependency edges between
 * tasks are not part of `TaskView`; the plan keeps the declared `depends_on`
 * of each lead, so the list states that gap instead of inventing a graph.
 */

import { useMemo, useState } from "react";

import { useAgentSessions } from "../api/agentSessions";
import { toWorkshopApiError } from "../api/client";
import { useTasks } from "../api/tasks";
import type { TaskPriority, TaskStatus } from "../api/types";
import { useWorkspaceStatus } from "../api/workspace";
import { AVAILABILITY_LABELS } from "../workspace/workspaceLabels";
import { TASK_PRIORITY_LABELS, TASK_STATUS_LABELS } from "./labels";
import { TaskCard } from "./TaskCard";

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

type ActorFilter = "all" | "human" | "operator" | "unassigned";

export function TaskWorkspace({
  investigationId,
}: {
  investigationId: string;
}) {
  const tasks = useTasks(investigationId);
  const sessions = useAgentSessions(investigationId);
  const workspace = useWorkspaceStatus(investigationId);
  const [status, setStatus] = useState<TaskStatus | "all">("all");
  const [priority, setPriority] = useState<TaskPriority | "all">("all");
  const [actor, setActor] = useState<ActorFilter>("all");
  const [text, setText] = useState("");

  const items = useMemo(() => tasks.data?.items ?? [], [tasks.data]);
  const visible = useMemo(() => {
    const needle = text.trim().toLocaleLowerCase("fr");
    return items
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
      })
      .sort(
        (left, right) =>
          PRIORITY_RANK[left.priority] - PRIORITY_RANK[right.priority] ||
          left.created_at.localeCompare(right.created_at),
      );
  }, [items, status, priority, actor, text]);

  const workspaceAvailable =
    workspace.data === undefined ? null : workspace.data.workspace_available;
  const operatorSessions = sessions.data ?? [];

  return (
    <section className="m24-card" aria-labelledby="m24-tasks-heading">
      <h3 id="m24-tasks-heading">Tâches</h3>
      <p className="m24-muted">
        Le graphe de dépendances entre tâches n’est pas exposé par le contrat
        API actuel ; les dépendances déclarées des pistes restent visibles dans
        le plan. Les sous-tâches sont visibles sur chaque carte.
      </p>

      {workspace.isPending ? (
        <p role="status" className="m24-muted">
          Vérification du workspace local…
        </p>
      ) : workspace.isError ? (
        <p role="alert" className="error-message">
          Statut du workspace indisponible : les actions qui écrivent dans le
          workspace sont masquées.
        </p>
      ) : workspaceAvailable === true ? (
        <p className="m24-muted">Workspace local disponible.</p>
      ) : (
        <p role="alert" className="error-message">
          Workspace local indisponible (
          {workspace.data === undefined
            ? "indisponible"
            : AVAILABILITY_LABELS[workspace.data.availability_code]}
          ) : les attributions, transferts et notes sont masqués.
        </p>
      )}

      <div className="m24-filters" role="group" aria-label="Filtres des tâches">
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
            onChange={(event) => setActor(event.target.value as ActorFilter)}
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
        <p className="empty-state">Aucune tâche ne correspond aux filtres.</p>
      ) : (
        <div className="m24-task-list">
          {visible.map((task) => (
            <TaskCard
              key={task.id}
              investigationId={investigationId}
              task={task}
              allTasks={items}
              sessions={operatorSessions}
              workspaceAvailable={workspaceAvailable}
            />
          ))}
        </div>
      )}
    </section>
  );
}
