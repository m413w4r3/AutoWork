/**
 * One task card of the M24 cockpit.
 *
 * Actions are rendered only when `taskActionAvailability` proves they are
 * executable for a verified human; every mutation sends the exact
 * `status_version` (CAS) and an `Idempotency-Key` through the frozen API
 * hooks. A `stale_task_version` refusal reloads the task projections and
 * states it, instead of silently retrying.
 */

import { useId, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { toWorkshopApiError } from "../api/client";
import type { AgentSessionView } from "../api/agentSessions";
import {
  tasksKeys,
  useRunTaskCommand,
  useTaskEvents,
  useTimeline,
  type TimelineItemV1,
  type TaskView,
} from "../api/tasks";
import { useTransferTask, type TransferResult } from "../api/transfers";
import { useWriteWorkspaceNote } from "../api/workspace";
import { describeTaskError } from "./errors";
import {
  actorRefLabel,
  ADAPTER_LABELS,
  BLOCKED_REASON_LABELS,
  formatTimestamp,
  listOrPlaceholder,
  orPlaceholder,
  TASK_ACTOR_LABELS,
  TASK_EVENT_LABELS,
  TASK_PRIORITY_LABELS,
  TASK_STATUS_LABELS,
  VERIFICATION_LABELS,
  TIMELINE_TYPE_LABELS,
} from "./labels";
import {
  taskActionAvailability,
  taskActionLabel,
  type TaskActionId,
} from "./taskActions";
import { TaskBridgePanel } from "./TaskBridgePanel";
import { OperatorsPanel } from "./OperatorsPanel";
import { TimelinePanel } from "./TimelinePanel";
import { timelineSummaryLabel } from "./timelinePresentation";
import { handleInvestigationLinkClick, investigationHref } from "../navigation";

type TransferDestination = "human" | "codex_cli" | "claude_code";
type SimpleCommand = "start" | "release" | "submit" | "unblock";
type ReasonAction = "interrupt_task" | "accept_result" | "request_changes";

export type TaskCardSubview = "summary" | "results" | "agent" | "history";

const TASK_SUBVIEWS: ReadonlyArray<{ id: TaskCardSubview; label: string }> = [
  { id: "summary", label: "Résumé" },
  { id: "results", label: "Résultats et preuves" },
  { id: "agent", label: "Agent et messages" },
  { id: "history", label: "Historique" },
];

/** Destructive actions carry the danger variant, apart from the primary ones. */
const DESTRUCTIVE_ACTIONS: ReadonlySet<TaskActionId> = new Set<TaskActionId>([
  "interrupt_task",
]);

const REASON_LABELS: Record<ReasonAction, string> = {
  interrupt_task: "Motif d’interruption de la tâche (obligatoire)",
  accept_result: "Motif d’acceptation (obligatoire)",
  request_changes: "Modifications demandées (obligatoire)",
};

const REASON_DONE: Record<ReasonAction, string> = {
  interrupt_task: "Interruption enregistrée.",
  accept_result: "Résultat accepté ; la tâche est terminée.",
  request_changes: "Modifications demandées ; la tâche repasse prête.",
};

function policyValue(
  task: TaskView,
  keys: readonly string[],
): string | string[] | null {
  for (const key of keys) {
    const value = task.execution_policy[key];
    if (typeof value === "string" && value.trim() !== "") return value;
    if (Array.isArray(value)) {
      const strings = value.filter(
        (item): item is string => typeof item === "string",
      );
      if (strings.length > 0) return strings;
    }
  }
  return null;
}

function PolicyValue({ value }: { value: string | string[] | null }) {
  if (value === null) return <>Non renseigné</>;
  if (Array.isArray(value))
    return (
      <>
        {value.map((item, index) => (
          <span key={`${item}-${index}`}>
            {index === 0 ? "" : " · "}
            {item}
          </span>
        ))}
      </>
    );
  return <>{value}</>;
}

function CopyableTaskValue({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      if (navigator.clipboard?.writeText === undefined) return;
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  return (
    <span className="m24-task-technical-value">
      <code>{value}</code>
      <button
        type="button"
        aria-label={`Copier ${label}`}
        onClick={() => void copy()}
      >
        Copier
      </button>
      <span role="status">{copied ? "Copié." : ""}</span>
    </span>
  );
}

function safeSourceUrl(value: unknown): value is string {
  return typeof value === "string" && /^https?:\/\//i.test(value);
}

function ResultEvidence({ item }: { item: TimelineItemV1 }) {
  const status = item.evidence
    ? "Preuve canonique"
    : item.item_type === "contribution"
      ? "Contribution agentique non vérifiée"
      : item.item_type === "bridge_consultation"
        ? "Avis Bridge — non probant"
        : "Élément de travail — non probant";
  return (
    <li className="m24-result-evidence">
      <div className="m24-task-card__header">
        <time dateTime={item.occurred_at}>
          {formatTimestamp(item.occurred_at)}
        </time>
        <span className="m24-badge">
          {TIMELINE_TYPE_LABELS[item.item_type]}
        </span>
        <span
          className={
            item.evidence
              ? "m24-badge m24-badge--evidence"
              : "m24-badge m24-badge--muted"
          }
        >
          {status}
        </span>
      </div>
      <p>{timelineSummaryLabel(item)}</p>
      {Object.entries(item.refs)
        .filter(([, value]) => safeSourceUrl(value))
        .map(([key, value]) => (
          <p key={`${item.object_id}-${key}`}>
            <a href={value} target="_blank" rel="noreferrer">
              Ouvrir la source
            </a>
          </p>
        ))}
      {Object.entries(item.refs).length === 0 && !item.summary ? null : (
        <details className="m24-timeline__technical">
          <summary>Détails techniques</summary>
          <dl className="m24-metadata m24-metadata--refs">
            <div>
              <dt>Résumé brut</dt>
              <dd>
                <code>{item.summary || "Non renseigné"}</code>
              </dd>
            </div>
            <div>
              <dt>Objet</dt>
              <dd>
                <code>{item.object_id}</code>
              </dd>
            </div>
            {Object.entries(item.refs).map(([key, value]) => (
              <div key={`${item.object_id}-${key}`}>
                <dt>{key.replaceAll("_", " ")}</dt>
                <dd>
                  <code>{value}</code>
                </dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </li>
  );
}

function notePathFor(task: TaskView): string {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const slug = task.task_key.replace(/[^A-Za-z0-9._-]+/g, "-");
  return `notes/humaines/tache-${slug}-${stamp}.md`;
}

function assignmentLabel(task: TaskView): string {
  if (task.assigned_actor_kind === null || task.assigned_actor_id === null) {
    return "Non attribuée";
  }
  return `${TASK_ACTOR_LABELS[task.assigned_actor_kind]} · ${actorRefLabel(
    task.assigned_actor_id,
  )}`;
}

export interface TaskCardProps {
  investigationId: string;
  task: TaskView;
  allTasks: readonly TaskView[];
  sessions: readonly AgentSessionView[];
  workspaceAvailable: boolean | null;
  activeSubview?: TaskCardSubview;
  onSubviewChange?: (subview: TaskCardSubview) => void;
  onTaskSelect?: (taskId: string) => void;
}

export function TaskCard({
  investigationId,
  task,
  allTasks,
  sessions,
  workspaceAvailable,
  activeSubview,
  onSubviewChange,
  onTaskSelect,
}: TaskCardProps) {
  const baseId = useId();
  const tabRefs = useRef(new Map<TaskCardSubview, HTMLButtonElement>());
  const queryClient = useQueryClient();
  const command = useRunTaskCommand(investigationId, task.id);
  const transfer = useTransferTask(investigationId, task.id);
  const note = useWriteWorkspaceNote(investigationId);
  const [internalSubview, setInternalSubview] =
    useState<TaskCardSubview>("summary");
  const currentSubview = activeSubview ?? internalSubview;
  const changeSubview = (subview: TaskCardSubview) => {
    setInternalSubview(subview);
    onSubviewChange?.(subview);
  };
  const events = useTaskEvents(
    investigationId,
    task.id,
    { limit: 20 },
    { enabled: currentSubview === "results" },
  );
  const timeline = useTimeline(
    investigationId,
    { limit: 50 },
    { enabled: currentSubview === "results" },
  );
  const [message, setMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [reasonAction, setReasonAction] = useState<ReasonAction | null>(null);
  const [reasonText, setReasonText] = useState("");
  const [noteOpen, setNoteOpen] = useState(false);
  const [noteText, setNoteText] = useState("");
  const [transferOpen, setTransferOpen] = useState(false);
  const [transferDestination, setTransferDestination] =
    useState<TransferDestination>("codex_cli");
  const [transferInstruction, setTransferInstruction] = useState("");

  const actions = taskActionAvailability(task, {
    workspaceAvailable,
    sessions,
  });
  const taskActions = actions.filter(
    (action) => action !== "interrupt_session" && action !== "resume_session",
  );
  const subtasks = allTasks.filter((item) => item.parent_task_id === task.id);
  const parent =
    task.parent_task_id === null
      ? undefined
      : allTasks.find((item) => item.id === task.parent_task_id);
  const busy = command.isPending || transfer.isPending || note.isPending;

  function finish(text: string) {
    setMessage(text);
    setErrorMessage(null);
  }

  function handleMutationError(error: unknown) {
    const apiError = toWorkshopApiError(error);
    setMessage(null);
    if (apiError.code === "stale_task_version") {
      setErrorMessage(
        "La tâche a changé depuis son chargement (version obsolète). Les données sont rechargées ; vérifiez l’état puis réessayez.",
      );
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.detail(investigationId, task.id),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.list(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
      return;
    }
    setErrorMessage(describeTaskError(apiError));
  }

  function runSimple(kind: SimpleCommand, done: string) {
    setMessage(null);
    setErrorMessage(null);
    const options = {
      onSuccess: () => finish(done),
      onError: handleMutationError,
    };
    const expected_status_version = task.status_version;
    if (kind === "start") {
      command.mutate({ command: "start", expected_status_version }, options);
    } else if (kind === "release") {
      command.mutate({ command: "release", expected_status_version }, options);
    } else if (kind === "submit") {
      command.mutate({ command: "submit", expected_status_version }, options);
    } else {
      command.mutate({ command: "unblock", expected_status_version }, options);
    }
  }

  function runTransfer(destination: TransferDestination, instruction = "") {
    setMessage(null);
    setErrorMessage(null);
    const trimmed = instruction.trim();
    const expected_status_version = task.status_version;
    const options = {
      onSuccess: (result: TransferResult) => {
        const version = result.handoff.version;
        if (destination === "human") {
          finish(`Tâche transférée à vous ; passation v${version} créée.`);
          return;
        }
        const target = ADAPTER_LABELS[destination];
        // `open_session: false` : l’attribution ne démarre aucun agent. La
        // session s’ouvre par l’action explicite rendue plus bas, seulement si
        // le sondage des capacités atteste le confinement isolé.
        if (result.session === null || result.session === undefined) {
          finish(
            `Passation v${version} créée pour ${target} — aucune session ouverte pour l’instant.`,
          );
        } else {
          finish(`Session ${target} ouverte ; passation v${version} créée.`);
        }
      },
      onError: handleMutationError,
    };
    if (destination === "human") {
      transfer.mutate(
        {
          to_actor_kind: "human",
          expected_status_version,
          ...(trimmed === "" ? {} : { instruction: trimmed }),
        },
        options,
      );
      return;
    }
    transfer.mutate(
      {
        to_actor_kind: "operator",
        to_adapter_kind: destination,
        open_session: false,
        expected_status_version,
        ...(trimmed === "" ? {} : { instruction: trimmed }),
      },
      options,
    );
  }

  function openReason(action: ReasonAction) {
    setReasonAction(action);
    setReasonText("");
    setMessage(null);
    setErrorMessage(null);
  }

  function confirmReason() {
    if (reasonAction === null) return;
    const reason = reasonText.trim();
    if (reason === "") return;
    const expected_status_version = task.status_version;
    const options = {
      onSuccess: () => {
        finish(
          reasonAction === "interrupt_task" &&
            task.status !== "running" &&
            task.status !== "awaiting_input"
            ? "Annulation de la tâche enregistrée."
            : REASON_DONE[reasonAction],
        );
        setReasonAction(null);
        setReasonText("");
      },
      onError: handleMutationError,
    };
    if (reasonAction === "interrupt_task") {
      command.mutate(
        { command: "cancel", expected_status_version, reason },
        options,
      );
    } else if (reasonAction === "accept_result") {
      command.mutate(
        { command: "accept_result", expected_status_version, reason },
        options,
      );
    } else {
      command.mutate(
        { command: "request_changes", expected_status_version, reason },
        options,
      );
    }
  }

  function submitNote() {
    const text = noteText.trim();
    if (text === "") return;
    setMessage(null);
    setErrorMessage(null);
    note.mutate(
      { path: notePathFor(task), expected_sha256: "new", text },
      {
        onSuccess: (result) => {
          finish(`Note enregistrée dans ${result.path}.`);
          setNoteOpen(false);
          setNoteText("");
        },
        onError: handleMutationError,
      },
    );
  }

  function onAction(action: TaskActionId) {
    switch (action) {
      case "take":
        runTransfer("human");
        return;
      case "assign_codex":
        runTransfer("codex_cli");
        return;
      case "assign_claude":
        runTransfer("claude_code");
        return;
      case "transfer":
        setTransferOpen(true);
        setErrorMessage(null);
        return;
      case "resume_task":
        runSimple("unblock", "Tâche reprise : elle est de nouveau prête.");
        return;
      case "start":
        runSimple("start", "Tâche démarrée.");
        return;
      case "release":
        runSimple("release", "Tâche libérée et de nouveau prête.");
        return;
      case "submit":
        runSimple("submit", "Résultat soumis pour revue humaine.");
        return;
      case "examine_results":
        changeSubview("results");
        return;
      case "accept_result":
        openReason("accept_result");
        return;
      case "request_changes":
        openReason("request_changes");
        return;
      case "interrupt_task":
        openReason("interrupt_task");
        return;
      case "add_note":
        setNoteOpen(true);
        return;
    }
  }

  function tabPanelId(subview: TaskCardSubview) {
    return `${baseId}-${subview}-panel`;
  }

  function handleTabKeyDown(
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (index + 1) % TASK_SUBVIEWS.length;
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (index - 1 + TASK_SUBVIEWS.length) % TASK_SUBVIEWS.length;
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = TASK_SUBVIEWS.length - 1;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    const next = TASK_SUBVIEWS[nextIndex];
    if (next === undefined) return;
    changeSubview(next.id);
    tabRefs.current.get(next.id)?.focus();
  }

  const resultItems = (timeline.data?.items ?? []).filter(
    (item) =>
      item.task_id === task.id &&
      ["tool_invocation", "observation", "contribution"].includes(
        item.item_type,
      ),
  );

  return (
    <article
      className="m24-task-card"
      id={`task-${task.id}`}
      aria-labelledby={`${baseId}-title`}
      tabIndex={-1}
    >
      <header className="m24-task-card__header">
        <h4 id={`${baseId}-title`}>{task.title}</h4>
        <span className={`m24-badge m24-badge--${task.status}`}>
          {TASK_STATUS_LABELS[task.status]}
        </span>
        <span className="m24-muted">{assignmentLabel(task)}</span>
      </header>

      <div className="m24-task-card__prompt">
        <p>
          <strong>Question :</strong> {orPlaceholder(task.question)}
        </p>
        <p>
          <strong>Objectif :</strong> {orPlaceholder(task.objective)}
        </p>
      </div>

      <div
        className="m24-task-card__actions"
        role="group"
        aria-label={`Actions pour ${task.title}`}
      >
        {taskActions
          .filter((action) => !DESTRUCTIVE_ACTIONS.has(action))
          .map((action) => (
            <button
              key={action}
              type="button"
              className="m24-action"
              onClick={() => onAction(action)}
              disabled={busy}
              title={
                action === "resume_task"
                  ? "Remettre une tâche bloquée en état prêt."
                  : undefined
              }
            >
              {taskActionLabel(action, task)}
            </button>
          ))}
      </div>
      <p className="m24-muted">
        Les attributions et transferts créent une passation versionnée ; la
        session Codex / Claude Code ne s’ouvre que si le sondage de capacités
        atteste le confinement isolé du CLI.
      </p>

      {taskActions.some((action) => DESTRUCTIVE_ACTIONS.has(action)) ? (
        <details className="m24-more-actions">
          <summary>Plus d’actions</summary>
          <div
            className="m24-task-card__actions"
            role="group"
            aria-label={`Actions secondaires pour ${task.title}`}
          >
            {taskActions
              .filter((action) => DESTRUCTIVE_ACTIONS.has(action))
              .map((action) => (
                <button
                  key={action}
                  type="button"
                  className="m24-action m24-action--danger"
                  onClick={() => onAction(action)}
                  disabled={busy}
                >
                  {taskActionLabel(action, task)}
                </button>
              ))}
          </div>
        </details>
      ) : null}

      <div
        className="m24-task-card__tablist"
        role="tablist"
        aria-label={`Vues de ${task.title}`}
      >
        {TASK_SUBVIEWS.map((subview, index) => (
          <button
            key={subview.id}
            ref={(element) => {
              if (element) tabRefs.current.set(subview.id, element);
              else tabRefs.current.delete(subview.id);
            }}
            id={`${baseId}-${subview.id}-tab`}
            type="button"
            role="tab"
            aria-selected={currentSubview === subview.id}
            aria-controls={tabPanelId(subview.id)}
            tabIndex={currentSubview === subview.id ? 0 : -1}
            className={
              currentSubview === subview.id
                ? "m24-task-tab m24-task-tab--active"
                : "m24-task-tab"
            }
            onClick={() => changeSubview(subview.id)}
            onKeyDown={(event) => handleTabKeyDown(event, index)}
          >
            {subview.label}
          </button>
        ))}
      </div>

      {TASK_SUBVIEWS.map((subview) => (
        <section
          key={subview.id}
          id={tabPanelId(subview.id)}
          role="tabpanel"
          aria-labelledby={`${baseId}-${subview.id}-tab`}
          tabIndex={0}
          hidden={currentSubview !== subview.id}
          className="m24-task-card__view"
        >
          {currentSubview === subview.id ? (
            <>
              {subview.id === "summary" ? (
                <>
                  <dl className="m24-metadata">
                    <div>
                      <dt>Statut</dt>
                      <dd>{TASK_STATUS_LABELS[task.status]}</dd>
                    </div>
                    <div>
                      <dt>Priorité</dt>
                      <dd>{TASK_PRIORITY_LABELS[task.priority]}</dd>
                    </div>
                    <div>
                      <dt>Vérification du résultat</dt>
                      <dd>
                        {VERIFICATION_LABELS[task.result_verification_status]}
                      </dd>
                    </div>
                    {task.blocked_reason === null ? null : (
                      <div>
                        <dt>Blocage</dt>
                        <dd>{BLOCKED_REASON_LABELS[task.blocked_reason]}</dd>
                      </div>
                    )}
                  </dl>
                  <dl className="m24-metadata m24-metadata--refs">
                    <div>
                      <dt>Contexte</dt>
                      <dd>
                        <PolicyValue
                          value={policyValue(task, [
                            "context",
                            "cti_justification",
                            "interest",
                          ])}
                        />
                      </dd>
                    </div>
                    <div>
                      <dt>Limites</dt>
                      <dd>
                        <PolicyValue value={policyValue(task, ["limits"])} />
                      </dd>
                    </div>
                    <div>
                      <dt>Critères de conclusion</dt>
                      <dd>
                        <PolicyValue
                          value={policyValue(task, ["conclusion_criteria"])}
                        />
                      </dd>
                    </div>
                    <div>
                      <dt>Préconditions</dt>
                      <dd>
                        {listOrPlaceholder(
                          Array.isArray(task.execution_policy.preconditions)
                            ? task.execution_policy.preconditions.filter(
                                (item): item is string =>
                                  typeof item === "string",
                              )
                            : [],
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Preuves de départ</dt>
                      <dd>
                        {task.evidence_refs.length === 0 ? (
                          "Aucune preuve de départ"
                        ) : (
                          <button
                            type="button"
                            className="m24-task-card__text-link"
                            onClick={() => changeSubview("results")}
                          >
                            {task.evidence_refs.length} preuve
                            {task.evidence_refs.length === 1 ? "" : "s"} de
                            départ · voir Résultats et preuves
                          </button>
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Échantillons</dt>
                      <dd>
                        {task.sample_refs.length === 0
                          ? "Aucun échantillon"
                          : (() => {
                              const href = investigationHref({
                                vue: "samples",
                              });
                              return (
                                <a
                                  href={href}
                                  onClick={(event) =>
                                    handleInvestigationLinkClick(event, href)
                                  }
                                >
                                  {task.sample_refs.length} échantillon
                                  {task.sample_refs.length === 1 ? "" : "s"} ·
                                  voir Échantillons et résultats
                                </a>
                              );
                            })()}
                      </dd>
                    </div>
                    <div>
                      <dt>Sorties attendues</dt>
                      <dd>{listOrPlaceholder(task.expected_outputs)}</dd>
                    </div>
                  </dl>
                  <details className="m24-task-technical-details">
                    <summary>Détails techniques</summary>
                    <dl className="m24-metadata m24-metadata--refs">
                      <div>
                        <dt>Clé de tâche</dt>
                        <dd>
                          <CopyableTaskValue
                            label="la clé de tâche"
                            value={task.task_key}
                          />
                        </dd>
                      </div>
                      <div>
                        <dt>Identifiant de tâche</dt>
                        <dd>
                          <CopyableTaskValue
                            label="l’identifiant de tâche"
                            value={task.id}
                          />
                        </dd>
                      </div>
                      <div>
                        <dt>Preuves de départ</dt>
                        <dd>
                          {task.evidence_refs.map((value) => (
                            <CopyableTaskValue
                              key={value}
                              label="la référence de preuve"
                              value={value}
                            />
                          ))}
                        </dd>
                      </div>
                      <div>
                        <dt>Références d’échantillons (SHA-256)</dt>
                        <dd>
                          {task.sample_refs.map((value) => (
                            <CopyableTaskValue
                              key={value}
                              label="la référence d’échantillon"
                              value={value}
                            />
                          ))}
                        </dd>
                      </div>
                      <div>
                        <dt>Claims</dt>
                        <dd>
                          {task.claim_refs.map((value) => (
                            <CopyableTaskValue
                              key={value}
                              label="la référence de claim"
                              value={value}
                            />
                          ))}
                        </dd>
                      </div>
                      <div>
                        <dt>Expiration du bail</dt>
                        <dd>
                          <CopyableTaskValue
                            label="l’expiration du bail"
                            value={task.lease_expires_at ?? "Non renseignée"}
                          />
                        </dd>
                      </div>
                      <div>
                        <dt>Version CAS</dt>
                        <dd>
                          <CopyableTaskValue
                            label="la version CAS"
                            value={String(task.status_version)}
                          />
                        </dd>
                      </div>
                      <div>
                        <dt>Capacités requises</dt>
                        <dd>
                          {task.required_capabilities.map((value) => (
                            <CopyableTaskValue
                              key={value}
                              label="la capacité requise"
                              value={value}
                            />
                          ))}
                        </dd>
                      </div>
                      <div>
                        <dt>Chemins autorisés</dt>
                        <dd>
                          {task.allowed_workspace_paths.map((value) => (
                            <CopyableTaskValue
                              key={value}
                              label="le chemin autorisé"
                              value={value}
                            />
                          ))}
                        </dd>
                      </div>
                    </dl>
                  </details>
                  {parent === undefined ? null : (
                    <p>
                      Sous-tâche de{" "}
                      <a
                        href={`#task-${parent.id}`}
                        onClick={(event) => {
                          if (onTaskSelect !== undefined) {
                            event.preventDefault();
                            onTaskSelect(parent.id);
                          }
                        }}
                      >
                        {parent.title} ({TASK_STATUS_LABELS[parent.status]})
                      </a>
                    </p>
                  )}
                  {subtasks.length === 0 ? null : (
                    <section aria-label="Sous-tâches">
                      <h5>Sous-tâches</h5>
                      <ul className="m24-subtasks">
                        {subtasks.map((subtask) => (
                          <li key={subtask.id}>
                            <a
                              href={`#task-${subtask.id}`}
                              onClick={(event) => {
                                if (onTaskSelect !== undefined) {
                                  event.preventDefault();
                                  onTaskSelect(subtask.id);
                                }
                              }}
                            >
                              {subtask.title}
                            </a>{" "}
                            <span className="m24-badge">
                              {TASK_STATUS_LABELS[subtask.status]}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </section>
                  )}
                </>
              ) : null}

              {subview.id === "results" ? (
                <section
                  className="m24-task-card__results"
                  aria-label="Résultats et preuves"
                >
                  <h5>
                    Résultat déclaré{" "}
                    <span className="m24-badge m24-badge--muted">
                      Non probant avant vérification
                    </span>
                  </h5>
                  <p>
                    État de vérification :{" "}
                    <strong>
                      {VERIFICATION_LABELS[task.result_verification_status]}
                    </strong>
                  </p>
                  {task.result_refs.length === 0 ? (
                    <p>Aucun résultat soumis.</p>
                  ) : (
                    <ul>
                      {task.result_refs.map((reference, index) => (
                        <li key={`${task.task_key}-result-${index}`}>
                          {Object.entries(reference)
                            .filter(([key]) => key !== "task_id")
                            .map(([key, value]) => (
                              <span
                                key={`${index}-${key}`}
                                className="m24-result-ref"
                              >
                                <strong>{key.replaceAll("_", " ")} :</strong>{" "}
                                {safeSourceUrl(value) ? (
                                  <a
                                    href={value}
                                    target="_blank"
                                    rel="noreferrer"
                                  >
                                    Ouvrir la source
                                  </a>
                                ) : (
                                  <code>
                                    {typeof value === "string"
                                      ? value
                                      : JSON.stringify(value)}
                                  </code>
                                )}
                              </span>
                            ))}
                        </li>
                      ))}
                    </ul>
                  )}
                  <h5>Analyses, observations et contributions liées</h5>
                  {timeline.isPending ? (
                    <p role="status">Chargement des résultats liés…</p>
                  ) : timeline.isError ? (
                    <p role="alert" className="error-message">
                      Éléments liés indisponibles :{" "}
                      {toWorkshopApiError(timeline.error).code}.
                    </p>
                  ) : resultItems.length === 0 ? (
                    <p>
                      Aucune analyse, observation ou contribution liée dans la
                      chronologie disponible.
                    </p>
                  ) : (
                    <ul className="m24-result-evidence-list">
                      {resultItems.map((item) => (
                        <ResultEvidence
                          key={`${item.item_type}-${item.object_id}`}
                          item={item}
                        />
                      ))}
                    </ul>
                  )}
                  <h5>Événements récents</h5>
                  {events.isPending ? (
                    <p role="status">Chargement du journal…</p>
                  ) : events.isError ? (
                    <p role="alert" className="error-message">
                      Journal indisponible :{" "}
                      {toWorkshopApiError(events.error).code}
                    </p>
                  ) : events.data.items.length === 0 ? (
                    <p>Aucun événement de tâche.</p>
                  ) : (
                    <ol className="m24-events">
                      {events.data.items.map((event) => (
                        <li key={event.id}>
                          <time dateTime={event.occurred_at}>
                            {formatTimestamp(event.occurred_at)}
                          </time>{" "}
                          <span className="m24-badge">
                            {TASK_EVENT_LABELS[event.event_type]}
                          </span>{" "}
                          {event.from_status === null ||
                          event.to_status === null
                            ? null
                            : `(${TASK_STATUS_LABELS[event.from_status]} → ${TASK_STATUS_LABELS[event.to_status]}) `}
                          <span>
                            {TASK_ACTOR_LABELS[event.actor.actor_kind]}
                          </span>
                          {event.actor.actor_id === "" ? null : (
                            <span>
                              {" "}
                              · {actorRefLabel(event.actor.actor_id)}
                            </span>
                          )}
                        </li>
                      ))}
                    </ol>
                  )}
                </section>
              ) : null}

              {subview.id === "agent" ? (
                <div className="m24-task-agent-context">
                  <OperatorsPanel
                    investigationId={investigationId}
                    taskId={task.id}
                    task={task}
                    sessionContext={sessions}
                    onViewResults={() => changeSubview("results")}
                    onTransfer={
                      taskActions.includes("transfer")
                        ? () => onAction("transfer")
                        : undefined
                    }
                  />
                  <TaskBridgePanel
                    investigationId={investigationId}
                    task={task}
                  />
                </div>
              ) : null}

              {subview.id === "history" ? (
                <TimelinePanel
                  investigationId={investigationId}
                  taskId={task.id}
                  title="Historique de la tâche"
                />
              ) : null}
            </>
          ) : null}
        </section>
      ))}

      {message === null ? null : (
        <p role="status" className="m24-message">
          {message}
        </p>
      )}
      {errorMessage === null ? null : (
        <p role="alert" className="error-message">
          {errorMessage}
        </p>
      )}

      {reasonAction === null ? null : (
        <div className="m24-inline-form">
          <label htmlFor={`${baseId}-reason`}>
            {reasonAction === "interrupt_task" &&
            task.status !== "running" &&
            task.status !== "awaiting_input"
              ? "Motif d’annulation de la tâche (obligatoire)"
              : REASON_LABELS[reasonAction]}
          </label>
          <textarea
            id={`${baseId}-reason`}
            value={reasonText}
            onChange={(event) => setReasonText(event.target.value)}
            rows={3}
          />
          <div className="m24-inline-form__buttons">
            <button
              type="button"
              className="m24-action"
              onClick={confirmReason}
              disabled={reasonText.trim() === "" || busy}
            >
              Confirmer
            </button>
            <button
              type="button"
              className="m24-action m24-action--quiet"
              onClick={() => setReasonAction(null)}
            >
              Annuler
            </button>
          </div>
        </div>
      )}

      {transferOpen ? (
        <div className="m24-inline-form">
          <fieldset>
            <legend>Destination du transfert</legend>
            {(
              [
                ["codex_cli", "Codex CLI"],
                ["claude_code", "Claude Code"],
                ["human", "Humain (vous)"],
              ] as const
            ).map(([value, label]) => (
              <label key={value} className="m24-radio">
                <input
                  type="radio"
                  name={`${baseId}-destination`}
                  value={value}
                  checked={transferDestination === value}
                  onChange={() => setTransferDestination(value)}
                />
                {label}
              </label>
            ))}
          </fieldset>
          <label htmlFor={`${baseId}-transfer-instruction`}>
            Instruction pour la session destination (facultative)
          </label>
          <textarea
            id={`${baseId}-transfer-instruction`}
            value={transferInstruction}
            onChange={(event) => setTransferInstruction(event.target.value)}
            rows={3}
          />
          <div className="m24-inline-form__buttons">
            <button
              type="button"
              className="m24-action"
              onClick={() => {
                runTransfer(transferDestination, transferInstruction);
                setTransferOpen(false);
                setTransferInstruction("");
              }}
              disabled={busy}
            >
              Confirmer le transfert
            </button>
            <button
              type="button"
              className="m24-action m24-action--quiet"
              onClick={() => setTransferOpen(false)}
            >
              Annuler
            </button>
          </div>
        </div>
      ) : null}

      {noteOpen ? (
        <div className="m24-inline-form">
          <label htmlFor={`${baseId}-note`}>
            Note (Markdown, non probante)
          </label>
          <textarea
            id={`${baseId}-note`}
            value={noteText}
            onChange={(event) => setNoteText(event.target.value)}
            rows={4}
          />
          <div className="m24-inline-form__buttons">
            <button
              type="button"
              className="m24-action"
              onClick={submitNote}
              disabled={noteText.trim() === "" || busy}
            >
              Enregistrer la note
            </button>
            <button
              type="button"
              className="m24-action m24-action--quiet"
              onClick={() => setNoteOpen(false)}
            >
              Annuler
            </button>
          </div>
        </div>
      ) : null}
    </article>
  );
}
