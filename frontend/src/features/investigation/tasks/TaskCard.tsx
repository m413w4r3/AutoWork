/**
 * One task card of the M24 cockpit.
 *
 * Actions are rendered only when `taskActionAvailability` proves they are
 * executable for a verified human; every mutation sends the exact
 * `status_version` (CAS) and an `Idempotency-Key` through the frozen API
 * hooks. A `stale_task_version` refusal reloads the task projections and
 * states it, instead of silently retrying.
 */

import { useId, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { toWorkshopApiError } from "../api/client";
import {
  useInterruptAgentSession,
  useResumeAgentSession,
  type AgentSessionView,
} from "../api/agentSessions";
import {
  tasksKeys,
  useRunTaskCommand,
  useTaskEvents,
  type TaskView,
} from "../api/tasks";
import { useTransferTask, type TransferResult } from "../api/transfers";
import { useWriteWorkspaceNote } from "../api/workspace";
import { describeTaskError } from "./errors";
import {
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
} from "./labels";
import {
  sessionForTask,
  taskActionAvailability,
  taskActionLabel,
  type TaskActionId,
} from "./taskActions";
import {
  OpenSessionAction,
  type OpenSessionFeedback,
} from "./OpenSessionAction";
import { sessionLaunchTarget } from "./sessionLaunch";
import { TaskBridgePanel } from "./TaskBridgePanel";

type TransferDestination = "human" | "codex_cli" | "claude_code";
type SimpleCommand = "start" | "release" | "submit" | "unblock";
type ReasonAction =
  "interrupt_task" | "accept_result" | "request_changes" | "interrupt_session";

const REASON_LABELS: Record<ReasonAction, string> = {
  interrupt_task: "Motif d’interruption (obligatoire)",
  accept_result: "Motif d’acceptation (obligatoire)",
  request_changes: "Modifications demandées (obligatoire)",
  interrupt_session: "Motif d’interruption de la session (obligatoire)",
};

const REASON_DONE: Record<ReasonAction, string> = {
  interrupt_task: "Interruption enregistrée.",
  accept_result: "Résultat accepté ; la tâche est terminée.",
  request_changes: "Modifications demandées ; la tâche repasse prête.",
  interrupt_session: "Interruption de session demandée.",
};

function notePathFor(task: TaskView): string {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const slug = task.task_key.replace(/[^A-Za-z0-9._-]+/g, "-");
  return `notes/humaines/tache-${slug}-${stamp}.md`;
}

function assignmentLabel(task: TaskView): string {
  if (task.assigned_actor_kind === null || task.assigned_actor_id === null) {
    return "Non attribuée";
  }
  return `${TASK_ACTOR_LABELS[task.assigned_actor_kind]} · ${task.assigned_actor_id}`;
}

export interface TaskCardProps {
  investigationId: string;
  task: TaskView;
  allTasks: readonly TaskView[];
  sessions: readonly AgentSessionView[];
  workspaceAvailable: boolean | null;
}

export function TaskCard({
  investigationId,
  task,
  allTasks,
  sessions,
  workspaceAvailable,
}: TaskCardProps) {
  const baseId = useId();
  const queryClient = useQueryClient();
  const command = useRunTaskCommand(investigationId, task.id);
  const transfer = useTransferTask(investigationId, task.id);
  const note = useWriteWorkspaceNote(investigationId);
  const session = sessionForTask(sessions, task.id);
  const interrupt = useInterruptAgentSession(
    investigationId,
    session?.id ?? "",
  );
  const resume = useResumeAgentSession(investigationId, session?.id ?? "");
  const [resultsOpen, setResultsOpen] = useState(false);
  const events = useTaskEvents(
    investigationId,
    task.id,
    { limit: 20 },
    { enabled: resultsOpen },
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
  // An operator assignment without any active or resumable session: the
  // handoff exists, so the cockpit may offer an explicit session opening.
  const launchTarget = session === null ? sessionLaunchTarget(task) : null;
  const subtasks = allTasks.filter((item) => item.parent_task_id === task.id);
  const parent =
    task.parent_task_id === null
      ? undefined
      : allTasks.find((item) => item.id === task.parent_task_id);
  const busy =
    command.isPending ||
    transfer.isPending ||
    note.isPending ||
    interrupt.isPending ||
    resume.isPending;

  function finish(text: string) {
    setMessage(text);
    setErrorMessage(null);
  }

  // The opening action disappears once the session exists or the task is
  // `running`; its feedback stays on this card, which survives both refetches.
  function showOpenSessionFeedback(feedback: OpenSessionFeedback) {
    if (feedback.kind === "success") {
      finish(feedback.text);
      return;
    }
    setMessage(null);
    setErrorMessage(feedback.text);
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
        finish(REASON_DONE[reasonAction]);
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
    } else if (reasonAction === "request_changes") {
      command.mutate(
        { command: "request_changes", expected_status_version, reason },
        options,
      );
    } else if (session !== null) {
      interrupt.mutate(reason, options);
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

  function resumeSession() {
    setMessage(null);
    setErrorMessage(null);
    resume.mutate(undefined, {
      onSuccess: () => finish("Session reprise par l’adaptateur."),
      onError: handleMutationError,
    });
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
        setResultsOpen((open) => !open);
        return;
      case "accept_result":
        openReason("accept_result");
        return;
      case "request_changes":
        openReason("request_changes");
        return;
      case "interrupt_session":
        openReason("interrupt_session");
        return;
      case "resume_session":
        resumeSession();
        return;
      case "interrupt_task":
        openReason("interrupt_task");
        return;
      case "add_note":
        setNoteOpen(true);
        return;
    }
  }

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
      </header>

      <dl className="m24-metadata">
        <div>
          <dt>Statut</dt>
          <dd>
            {TASK_STATUS_LABELS[task.status]} · version {task.status_version}
          </dd>
        </div>
        <div>
          <dt>Priorité</dt>
          <dd>{TASK_PRIORITY_LABELS[task.priority]}</dd>
        </div>
        <div>
          <dt>Assignation</dt>
          <dd>{assignmentLabel(task)}</dd>
        </div>
        <div>
          <dt>Clé / identifiant</dt>
          <dd>
            <code>{task.task_key}</code> ·{" "}
            <code title={task.id}>{task.id.slice(0, 8)}…</code>
          </dd>
        </div>
        <div>
          <dt>Vérification du résultat</dt>
          <dd>{VERIFICATION_LABELS[task.result_verification_status]}</dd>
        </div>
        <div>
          <dt>Expiration du bail</dt>
          <dd>{formatTimestamp(task.lease_expires_at)}</dd>
        </div>
        {task.blocked_reason === null ? null : (
          <div>
            <dt>Blocage</dt>
            <dd>{BLOCKED_REASON_LABELS[task.blocked_reason]}</dd>
          </div>
        )}
      </dl>

      <p>
        <strong>Question :</strong> {orPlaceholder(task.question)}
      </p>
      <p>
        <strong>Objectif :</strong> {orPlaceholder(task.objective)}
      </p>

      <dl className="m24-metadata m24-metadata--refs">
        <div>
          <dt>Claims</dt>
          <dd>{listOrPlaceholder(task.claim_refs)}</dd>
        </div>
        <div>
          <dt>Preuves de départ</dt>
          <dd>{listOrPlaceholder(task.evidence_refs)}</dd>
        </div>
        <div>
          <dt>Échantillons</dt>
          <dd>{listOrPlaceholder(task.sample_refs)}</dd>
        </div>
        <div>
          <dt>Capacités requises</dt>
          <dd>{listOrPlaceholder(task.required_capabilities)}</dd>
        </div>
        <div>
          <dt>Sorties attendues</dt>
          <dd>{listOrPlaceholder(task.expected_outputs)}</dd>
        </div>
      </dl>

      {parent === undefined ? null : (
        <p>
          Sous-tâche de{" "}
          <a href={`#task-${parent.id}`}>
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
                <a href={`#task-${subtask.id}`}>{subtask.title}</a>{" "}
                <span className="m24-badge">
                  {TASK_STATUS_LABELS[subtask.status]}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div
        className="m24-task-card__actions"
        role="group"
        aria-label={`Actions pour ${task.title}`}
      >
        {actions.map((action) => (
          <button
            key={action}
            type="button"
            className="m24-action"
            onClick={() => onAction(action)}
            disabled={busy}
            aria-expanded={
              action === "examine_results" ? resultsOpen : undefined
            }
            title={
              action === "resume_task"
                ? "Remettre une tâche bloquée en état prêt."
                : action === "resume_session"
                  ? "Reprendre la session de l’agent si l’adaptateur le prouve."
                  : undefined
            }
          >
            {taskActionLabel(action, task)}
          </button>
        ))}
      </div>
      <p className="m24-muted">
        Les attributions et transferts créent une passation versionnée ; la
        session Codex / Claude Code s’ouvre ensuite par une action explicite,
        uniquement si le sondage des capacités atteste le confinement isolé du
        CLI. L’attribution seule ne démarre aucun agent.
      </p>

      <TaskBridgePanel investigationId={investigationId} task={task} />

      {launchTarget === null ? null : (
        <div className="m24-task-card__session-launch">
          <OpenSessionAction
            investigationId={investigationId}
            target={launchTarget}
            onFeedback={showOpenSessionFeedback}
          />
        </div>
      )}

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
            {REASON_LABELS[reasonAction]}
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
          <label htmlFor={`${baseId}-note`}>Note (Markdown, non-preuve)</label>
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

      {resultsOpen ? (
        <section
          className="m24-task-card__results"
          aria-label="Résultats de la tâche"
        >
          <h5>
            Résultats soumis <span className="m24-badge">Non preuve</span>
          </h5>
          <p className="m24-muted">
            Références déclarées par l’exécutant : elles ne constituent pas une
            observation canonique. Seules les contributions importées puis
            vérifiées font foi.
          </p>
          {task.result_refs.length === 0 ? (
            <p>Aucun résultat soumis.</p>
          ) : (
            <ul>
              {task.result_refs.map((reference, index) => (
                <li key={`${task.id}-result-${index}`}>
                  <code>{JSON.stringify(reference)}</code>
                </li>
              ))}
            </ul>
          )}
          <h5>Journal borné (20 derniers événements, non-preuve)</h5>
          {events.isPending ? (
            <p role="status">Chargement du journal…</p>
          ) : events.isError ? (
            <p role="alert" className="error-message">
              Journal indisponible : {toWorkshopApiError(events.error).code}
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
                  {event.from_status === null || event.to_status === null
                    ? null
                    : `(${TASK_STATUS_LABELS[event.from_status]} → ${TASK_STATUS_LABELS[event.to_status]}) `}
                  <span>{TASK_ACTOR_LABELS[event.actor.actor_kind]}</span>
                  {event.actor.actor_id === "" ? null : (
                    <span> · {event.actor.actor_id}</span>
                  )}
                </li>
              ))}
            </ol>
          )}
        </section>
      ) : null}
    </article>
  );
}
