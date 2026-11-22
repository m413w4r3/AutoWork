/**
 * Active-operator panel: one row per Codex CLI / Claude Code session.
 *
 * Only facts the frozen contract exposes are shown. Per-session operation and
 * budget counters and raw CLI logs are not part of `AgentSessionView`; the
 * panel states that instead of inventing them, and offers the bounded task
 * event journal on demand. Instructions go through the persistent human
 * enqueue route, so a busy agent keeps them queued.
 */

import { useId, useState } from "react";

import {
  useAgentSessions,
  useEnqueueAgentInstruction,
  useInterruptAgentSession,
  useResumeAgentSession,
  type AgentSessionView,
} from "../api/agentSessions";
import { toWorkshopApiError } from "../api/client";
import { useTaskEvents, useTasks, type TaskView } from "../api/tasks";
import type { TaskStatus } from "../api/types";
import { describeTaskError } from "./errors";
import {
  ADAPTER_LABELS,
  BLOCKED_REASON_LABELS,
  formatDuration,
  formatTimestamp,
  SESSION_STATE_LABELS,
  shortSha,
  TASK_ACTOR_LABELS,
  TASK_EVENT_LABELS,
  TASK_STATUS_LABELS,
  VERIFICATION_LABELS,
} from "./labels";
import { ACTIVE_SESSION_STATES, RESUMABLE_SESSION_STATES } from "./taskActions";

const TERMINAL_TASK_STATUSES: ReadonlySet<TaskStatus> = new Set<TaskStatus>([
  "completed",
  "cancelled",
  "failed",
]);

function SessionRow({
  investigationId,
  session,
  task,
}: {
  investigationId: string;
  session: AgentSessionView;
  task: TaskView | undefined;
}) {
  const baseId = useId();
  const enqueue = useEnqueueAgentInstruction(investigationId, session.id);
  const interrupt = useInterruptAgentSession(investigationId, session.id);
  const resume = useResumeAgentSession(investigationId, session.id);
  const [instruction, setInstruction] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [interruptOpen, setInterruptOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [logsOpen, setLogsOpen] = useState(false);
  const events = useTaskEvents(
    investigationId,
    session.task_id,
    { limit: 20 },
    { enabled: logsOpen },
  );

  const active = ACTIVE_SESSION_STATES.has(session.state);
  const interruptible =
    session.state === "running" || session.state === "awaiting_instruction";
  const resumable = RESUMABLE_SESSION_STATES.has(session.state);
  const taskTerminal =
    task !== undefined && TERMINAL_TASK_STATUSES.has(task.status);
  const durationEnd =
    session.closed_at ?? (active ? null : session.last_activity_at);
  const blocker =
    task?.blocked_reason != null
      ? BLOCKED_REASON_LABELS[task.blocked_reason]
      : (session.close_reason ?? "Aucun");
  const submitted =
    task === undefined || task.result_refs.length === 0
      ? "Aucun"
      : `${task.result_refs.length} référence(s) · ${VERIFICATION_LABELS[task.result_verification_status]}`;

  function finish(text: string) {
    setMessage(text);
    setErrorMessage(null);
  }

  function handleError(error: unknown) {
    setMessage(null);
    setErrorMessage(describeTaskError(toWorkshopApiError(error)));
  }

  function sendInstruction() {
    const text = instruction.trim();
    if (text === "") return;
    setMessage(null);
    setErrorMessage(null);
    enqueue.mutate(
      { text },
      {
        onSuccess: (result) => {
          finish(
            result.delivered_live
              ? "Instruction délivrée à la session."
              : `Instruction mise en file persistante (position ${result.queue_position}).`,
          );
          setInstruction("");
        },
        onError: handleError,
      },
    );
  }

  function confirmInterrupt() {
    const text = reason.trim();
    if (text === "") return;
    setMessage(null);
    setErrorMessage(null);
    interrupt.mutate(text, {
      onSuccess: () => {
        finish("Interruption demandée ; la session s’arrête.");
        setInterruptOpen(false);
        setReason("");
      },
      onError: handleError,
    });
  }

  function resumeSession() {
    setMessage(null);
    setErrorMessage(null);
    resume.mutate(undefined, {
      onSuccess: () => finish("Session reprise par l’adaptateur."),
      onError: handleError,
    });
  }

  return (
    <li className="m24-operator">
      <header className="m24-operator__header">
        <h5>
          {ADAPTER_LABELS[session.adapter_kind]}
          {task === undefined ? null : ` · ${task.title}`}
        </h5>
        <span className="m24-badge">{SESSION_STATE_LABELS[session.state]}</span>
      </header>
      <dl className="m24-metadata">
        <div>
          <dt>Fournisseur</dt>
          <dd>{ADAPTER_LABELS[session.adapter_kind]}</dd>
        </div>
        <div>
          <dt>Tâche</dt>
          <dd>
            {task === undefined ? "Tâche inconnue" : task.title}{" "}
            <code>{session.task_id}</code>
          </dd>
        </div>
        <div>
          <dt>Statut</dt>
          <dd>{SESSION_STATE_LABELS[session.state]}</dd>
        </div>
        <div>
          <dt>Durée</dt>
          <dd>{formatDuration(session.opened_at, durationEnd)}</dd>
        </div>
        <div>
          <dt>Dernière activité</dt>
          <dd>{formatTimestamp(session.last_activity_at)}</dd>
        </div>
        <div>
          <dt>Opérations</dt>
          <dd>Non exposées par le contrat M24</dd>
        </div>
        <div>
          <dt>Budget</dt>
          <dd>Non exposé par le contrat M24</dd>
        </div>
        <div>
          <dt>Checkpoint</dt>
          <dd>
            {session.checkpoint_ref ?? "Aucun"}{" "}
            {session.checkpoint_sha256 === null
              ? null
              : `(${shortSha(session.checkpoint_sha256)})`}
          </dd>
        </div>
        <div>
          <dt>Blocage</dt>
          <dd>{blocker}</dd>
        </div>
        <div>
          <dt>Résultat soumis</dt>
          <dd>{submitted}</dd>
        </div>
      </dl>

      {session.state === "lost" ? (
        <p className="m24-warning" role="status">
          Session perdue : la réconciliation hôte précède toute reprise. Si
          l’adaptateur ne peut pas reprendre, une nouvelle session doit être
          ouverte avec une nouvelle passation.
        </p>
      ) : null}

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

      {interruptible ? (
        <div className="m24-inline-form">
          <label htmlFor={`${baseId}-instruction`}>
            Instruction en cours de session (mise en file persistante si l’agent
            est occupé)
          </label>
          <textarea
            id={`${baseId}-instruction`}
            value={instruction}
            onChange={(event) => setInstruction(event.target.value)}
            rows={3}
          />
          <div className="m24-inline-form__buttons">
            <button
              type="button"
              className="m24-action"
              onClick={sendInstruction}
              disabled={instruction.trim() === "" || enqueue.isPending}
            >
              Envoyer l’instruction
            </button>
            {interruptOpen ? null : (
              <button
                type="button"
                className="m24-action m24-action--quiet"
                onClick={() => setInterruptOpen(true)}
              >
                Interrompre la session
              </button>
            )}
          </div>
          {interruptOpen ? (
            <div className="m24-inline-form">
              <label htmlFor={`${baseId}-reason`}>
                Motif d’interruption (obligatoire)
              </label>
              <textarea
                id={`${baseId}-reason`}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                rows={2}
              />
              <div className="m24-inline-form__buttons">
                <button
                  type="button"
                  className="m24-action"
                  onClick={confirmInterrupt}
                  disabled={reason.trim() === "" || interrupt.isPending}
                >
                  Confirmer l’interruption
                </button>
                <button
                  type="button"
                  className="m24-action m24-action--quiet"
                  onClick={() => setInterruptOpen(false)}
                >
                  Annuler
                </button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {resumable && !taskTerminal ? (
        <div className="m24-inline-form__buttons">
          <button
            type="button"
            className="m24-action"
            onClick={resumeSession}
            disabled={resume.isPending}
          >
            Reprendre la session
          </button>
        </div>
      ) : null}
      {resumable && taskTerminal ? (
        <p className="m24-muted" role="note">
          Reprise masquée : la tâche est terminée. Une nouvelle session exige
          une nouvelle passation.
        </p>
      ) : null}

      <div className="m24-inline-form__buttons">
        <button
          type="button"
          className="m24-action m24-action--quiet"
          onClick={() => setLogsOpen((open) => !open)}
          aria-expanded={logsOpen}
        >
          {logsOpen ? "Masquer le journal borné" : "Afficher le journal borné"}
        </button>
      </div>
      {logsOpen ? (
        <div className="m24-operator__logs">
          <h6>Journal borné (20 derniers événements de tâche, non-preuve)</h6>
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
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}
    </li>
  );
}

export function OperatorsPanel({
  investigationId,
}: {
  investigationId: string;
}) {
  const sessions = useAgentSessions(investigationId);
  const tasks = useTasks(investigationId);
  const taskById = new Map<string, TaskView>(
    (tasks.data?.items ?? []).map((task) => [task.id, task]),
  );
  const items = sessions.data ?? [];
  const active = items.filter((session) =>
    ACTIVE_SESSION_STATES.has(session.state),
  );
  const resumable = items.filter((session) =>
    RESUMABLE_SESSION_STATES.has(session.state),
  );
  const sessionError = sessions.isError
    ? toWorkshopApiError(sessions.error)
    : null;

  return (
    <section className="m24-card" aria-labelledby="m24-operators-heading">
      <h3 id="m24-operators-heading">Opérateurs actifs</h3>
      <p className="m24-muted">
        Une session Codex CLI ou Claude Code par tâche. Les capacités affichées
        (reprise, checkpoint) sont celles que le serveur expose ; rien n’est
        déduit d’un adaptateur présumé.
      </p>

      {sessions.isPending ? (
        <p role="status">Chargement des sessions…</p>
      ) : sessionError !== null ? (
        <p role="alert" className="error-message">
          {sessionError.code === "agent_sessions_unavailable"
            ? "Agent absent : les sessions d’opérateurs sont indisponibles (503)."
            : `Sessions indisponibles : ${describeTaskError(sessionError)}`}
        </p>
      ) : active.length === 0 ? (
        <p className="empty-state">Aucun opérateur actif.</p>
      ) : (
        <ul className="m24-operator-list">
          {active.map((session) => (
            <SessionRow
              key={session.id}
              investigationId={investigationId}
              session={session}
              task={taskById.get(session.task_id)}
            />
          ))}
        </ul>
      )}

      {resumable.length === 0 ? null : (
        <section aria-labelledby="m24-operators-resumable">
          <h4 id="m24-operators-resumable">
            Sessions arrêtées ou perdues ({resumable.length})
          </h4>
          <ul className="m24-operator-list">
            {resumable.map((session) => (
              <SessionRow
                key={session.id}
                investigationId={investigationId}
                session={session}
                task={taskById.get(session.task_id)}
              />
            ))}
          </ul>
        </section>
      )}
    </section>
  );
}
