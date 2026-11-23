/**
 * Active-operator panel: one row per Codex CLI / Claude Code session.
 *
 * Session facts come from `AgentSessionView`; the journal-derived projection
 * (`GET …/agent-sessions/{id}/summary`, lot B6) supplies operations, budget
 * sums, the envelope budget, the broker view, the handoff freshness and the
 * human instructions. The broker block is labelled as derived from the
 * journal — no live broker state exists to read — and nothing is inferred.
 * Instructions go through the persistent human enqueue route, so a busy
 * agent keeps them queued.
 */

import { useId, useState } from "react";

import {
  useAgentSessionSummary,
  useAgentSessions,
  useEnqueueAgentInstruction,
  useInterruptAgentSession,
  useResumeAgentSession,
  type AgentSessionBudgetView,
  type AgentSessionBrokerView,
  type AgentSessionEnvelopeBudgetView,
  type AgentSessionHandoffFreshnessView,
  type AgentSessionHandoffSummaryView,
  type AgentSessionInstructionWindowView,
  type AgentSessionOperationView,
  type AgentSessionOperationWindowView,
  type AgentSessionView,
} from "../api/agentSessions";
import { toWorkshopApiError } from "../api/client";
import { useTaskEvents, useTasks, type TaskView } from "../api/tasks";
import type {
  AgentSessionState,
  BudgetDimension,
  TaskStatus,
} from "../api/types";
import { BUDGET_DIMENSION_LABELS } from "../overview/overviewPresentation";
import { INVOCATION_STATUS_LABELS } from "../samples/samplesLabels";
import { describeTaskError } from "./errors";
import {
  actorRefLabel,
  ADAPTER_LABELS,
  BLOCKED_REASON_LABELS,
  formatBudgetAmount,
  formatDuration,
  formatTimestamp,
  HANDOFF_FRESHNESS_LABELS,
  INSTRUCTION_JOURNAL_STATE_LABELS,
  INSTRUCTION_SUBMISSION_PENDING_LABEL,
  SESSION_STATE_LABELS,
  shortSha,
  TASK_ACTOR_LABELS,
  TASK_EVENT_LABELS,
  TASK_STATUS_LABELS,
  VERIFICATION_LABELS,
} from "./labels";
import {
  OpenSessionAction,
  type OpenSessionFeedback,
} from "./OpenSessionAction";
import { sessionLaunchTarget, type SessionLaunchTarget } from "./sessionLaunch";
import {
  ACTIVE_SESSION_STATES,
  RESUMABLE_SESSION_STATES,
  sessionForTask,
} from "./taskActions";

const TERMINAL_TASK_STATUSES: ReadonlySet<TaskStatus> = new Set<TaskStatus>([
  "completed",
  "cancelled",
  "failed",
]);

const BUDGET_DIMENSIONS: readonly BudgetDimension[] = [
  "bridge_turns",
  "vt_units",
  "shodan_credits",
  "downloads",
  "tool_seconds",
  "context_tokens",
  "context_bytes",
];

/** Same colour semantics as the task status badge (F12). */
const SESSION_BADGE_VARIANTS: Record<AgentSessionState, string> = {
  provisioning: "m24-badge--running",
  ready: "m24-badge--running",
  running: "m24-badge--running",
  awaiting_instruction: "m24-badge--awaiting_review",
  interrupt_requested: "m24-badge--awaiting_review",
  reconciling: "m24-badge--awaiting_review",
  stopped: "m24-badge--muted",
  lost: "m24-badge--blocked",
  closed: "m24-badge--muted",
  failed: "m24-badge--failed",
};

function sessionBadgeClass(state: AgentSessionState): string {
  return `m24-badge ${SESSION_BADGE_VARIANTS[state]}`;
}

function operationBudgetText(operation: AgentSessionOperationView): string {
  const parts: string[] = [];
  for (const dimension of BUDGET_DIMENSIONS) {
    const consumed = operation.consumed_budget[dimension];
    const reserved = operation.reserved_budget[dimension];
    if (consumed === undefined && reserved === undefined) continue;
    parts.push(
      `${BUDGET_DIMENSION_LABELS[dimension]} : ${formatBudgetAmount(consumed ?? 0)} consommé, ${formatBudgetAmount(reserved ?? 0)} réservé`,
    );
  }
  return parts.length === 0 ? "aucun budget engagé" : parts.join(" · ");
}

function OperationList({
  window,
}: {
  window: AgentSessionOperationWindowView;
}) {
  return (
    <section aria-label="Opérations journalisées">
      <h6>Opérations journalisées ({window.total})</h6>
      {window.items.length === 0 ? (
        <p>Aucune opération journalisée pour cette session.</p>
      ) : (
        <ol className="m24-events">
          {window.items.map((operation) => (
            <li key={operation.invocation_id}>
              <time dateTime={operation.created_at}>
                {formatTimestamp(operation.created_at)}
              </time>{" "}
              <code>{operation.tool_name}</code>{" "}
              <span className="m24-badge">
                {INVOCATION_STATUS_LABELS[operation.status]}
              </span>{" "}
              <span>
                {operation.actor_kind === null
                  ? "Auteur inconnu"
                  : TASK_ACTOR_LABELS[operation.actor_kind]}
              </span>{" "}
              <span className="m24-muted">
                — {operationBudgetText(operation)}
              </span>
            </li>
          ))}
        </ol>
      )}
      {window.truncated ? (
        <p className="m24-muted">
          Fenêtre bornée aux {window.limit} opérations les plus récentes (total{" "}
          {window.total}).
        </p>
      ) : null}
    </section>
  );
}

function envelopeBudgetLine(
  dimension: BudgetDimension,
  envelope: AgentSessionEnvelopeBudgetView,
): string {
  const limit = envelope.limits?.[dimension] ?? null;
  const remaining = envelope.remaining?.[dimension] ?? null;
  const consumed = envelope.consumed?.[dimension] ?? 0;
  const reserved = envelope.reserved?.[dimension] ?? 0;
  return `${formatBudgetAmount(consumed)} consommé · ${formatBudgetAmount(reserved)} réservé · plafond ${formatBudgetAmount(limit)} · restant ${formatBudgetAmount(remaining)}`;
}

function BudgetBlock({ budget }: { budget: AgentSessionBudgetView }) {
  const sessionLines = BUDGET_DIMENSIONS.filter(
    (dimension) =>
      budget.session.consumed[dimension] !== 0 ||
      budget.session.reserved[dimension] !== 0,
  );
  return (
    <section aria-label="Budget de la session">
      <h6>Budget de la session et enveloppe</h6>
      <p className="m24-muted">
        {budget.session.invocation_count} invocation(s) journalisée(s) pour
        cette session.
      </p>
      {sessionLines.length === 0 ? (
        <p>Aucune consommation de budget enregistrée pour cette session.</p>
      ) : (
        <dl className="m24-metadata">
          {sessionLines.map((dimension) => (
            <div key={dimension}>
              <dt>{BUDGET_DIMENSION_LABELS[dimension]}</dt>
              <dd>
                {formatBudgetAmount(budget.session.consumed[dimension])}{" "}
                consommé ·{" "}
                {formatBudgetAmount(budget.session.reserved[dimension])} réservé
              </dd>
            </div>
          ))}
        </dl>
      )}
      {budget.envelope.present ? (
        <dl className="m24-metadata">
          <div>
            <dt>Enveloppe approuvée</dt>
            <dd>
              version {budget.envelope.version ?? "inconnue"} ·{" "}
              {formatTimestamp(budget.envelope.approved_at)}
            </dd>
          </div>
          {BUDGET_DIMENSIONS.map((dimension) => (
            <div key={dimension}>
              <dt>{BUDGET_DIMENSION_LABELS[dimension]}</dt>
              <dd>{envelopeBudgetLine(dimension, budget.envelope)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p>Aucune enveloppe approuvée pour cette enquête.</p>
      )}
    </section>
  );
}

function BrokerBlock({ broker }: { broker: AgentSessionBrokerView }) {
  return (
    <section aria-label="État du broker">
      <h6>
        Broker — <span className="m24-badge">Dérivé du journal</span>
      </h6>
      <p className="m24-muted">
        Aucun état broker en direct n’est lu : ces valeurs sont dérivées des
        invocations d’outils signées du journal.
      </p>
      <dl className="m24-metadata">
        <div>
          <dt>Appels signés</dt>
          <dd>{broker.signed_call_count}</dd>
        </div>
        <div>
          <dt>Revue humaine requise</dt>
          <dd>
            {broker.needs_review_count}
            {broker.reconciliation_required
              ? " — réconciliation requise"
              : null}
          </dd>
        </div>
        <div>
          <dt>Dernier appel signé</dt>
          <dd>
            {broker.last_signed_call === null
              ? "Aucun"
              : `${broker.last_signed_call.tool_name} · ${INVOCATION_STATUS_LABELS[broker.last_signed_call.status]} · ${formatTimestamp(broker.last_signed_call.created_at)}`}
          </dd>
        </div>
      </dl>
    </section>
  );
}

function freshnessText(
  freshness: AgentSessionHandoffFreshnessView | null,
): string {
  if (freshness === null) return "Non vérifiée";
  if (!freshness.checked) return `Non vérifiée (${freshness.reason})`;
  const label = HANDOFF_FRESHNESS_LABELS[freshness.state];
  return freshness.diffs.length === 0
    ? label
    : `${label} — ${freshness.diffs.length} référence(s) modifiée(s)`;
}

function HandoffBlock({
  handoff,
}: {
  handoff: AgentSessionHandoffSummaryView | null;
}) {
  return (
    <section aria-label="Dernière passation">
      <h6>Dernière passation</h6>
      {handoff === null ? (
        <p>Aucune passation liée à cette session.</p>
      ) : (
        <dl className="m24-metadata">
          <div>
            <dt>Passation</dt>
            <dd>
              <code title={handoff.handoff_id} className="m24-id-ref">
                {shortSha(handoff.handoff_id)}
              </code>{" "}
              · version {handoff.version}
            </dd>
          </div>
          <div>
            <dt>SHA-256</dt>
            <dd>
              <code>{handoff.sha256}</code>
            </dd>
          </div>
          <div>
            <dt>Créée</dt>
            <dd>{formatTimestamp(handoff.created_at)}</dd>
          </div>
          <div>
            <dt>Acteurs</dt>
            <dd>
              {TASK_ACTOR_LABELS[handoff.from_actor.kind]}{" "}
              {actorRefLabel(handoff.from_actor.actor_id)} →{" "}
              {TASK_ACTOR_LABELS[handoff.to_actor.kind]}{" "}
              {actorRefLabel(handoff.to_actor.actor_id)}
            </dd>
          </div>
          <div>
            <dt>Fraîcheur</dt>
            <dd>{freshnessText(handoff.freshness)}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}

function InstructionBlock({
  window,
}: {
  window: AgentSessionInstructionWindowView;
}) {
  return (
    <section aria-label="Instructions">
      <h6>Instructions ({window.total})</h6>
      {window.items.length === 0 ? (
        <p>Aucune instruction humaine pour cette session.</p>
      ) : (
        <ul className="m24-events">
          {window.items.map((instruction) => (
            <li key={instruction.instruction_id}>
              {instruction.state === "acknowledged" ? (
                <span aria-hidden="true">✓ </span>
              ) : instruction.state === "queued" ||
                instruction.state === "delivering" ? (
                <span aria-hidden="true">… </span>
              ) : null}
              <span className="m24-badge">
                {INSTRUCTION_JOURNAL_STATE_LABELS[instruction.state]}
              </span>{" "}
              {instruction.state === "queued" &&
              instruction.queue_position > 0 ? (
                <span className="m24-muted">
                  position {instruction.queue_position} ·{" "}
                </span>
              ) : null}
              <span>{instructionPreview(instruction.text)}</span>
              {instruction.state === "delivered" &&
              instruction.delivery_mode === "next_run" ? (
                <span className="m24-muted"> · prochaine exécution</span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {window.truncated ? (
        <p className="m24-muted">
          Fenêtre bornée aux {window.limit} dernières instructions (total{" "}
          {window.total}).
        </p>
      ) : null}
    </section>
  );
}

function instructionPreview(text: string): string {
  const limit = 160;
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
}

function InstructionTechnicalDetails({
  window,
}: {
  window: AgentSessionInstructionWindowView;
}) {
  if (window.items.length === 0) return null;
  return (
    <section aria-label="Détails techniques des instructions">
      <h6>Détails des instructions</h6>
      <ol className="m24-events">
        {window.items.map((instruction) => (
          <li key={instruction.instruction_id}>
            <p>{instruction.text}</p>
            <dl className="m24-metadata">
              <div>
                <dt>SHA-256 du texte</dt>
                <dd>
                  <code>{instruction.text_sha256}</code>
                </dd>
              </div>
              <div>
                <dt>Créée</dt>
                <dd>{formatTimestamp(instruction.created_at)}</dd>
              </div>
              <div>
                <dt>Mise en file</dt>
                <dd>{formatTimestamp(instruction.queued_at)}</dd>
              </div>
              {instruction.delivered_at === null ? null : (
                <div>
                  <dt>Transmise</dt>
                  <dd>{formatTimestamp(instruction.delivered_at)}</dd>
                </div>
              )}
              {instruction.acknowledged_at === null ? null : (
                <div>
                  <dt>Acquittée</dt>
                  <dd>{formatTimestamp(instruction.acknowledged_at)}</dd>
                </div>
              )}
              <div>
                <dt>Tentatives de livraison</dt>
                <dd>{instruction.delivery_attempts}</dd>
              </div>
              <div>
                <dt>Mode de livraison</dt>
                <dd>
                  {instruction.delivery_mode === "live"
                    ? "Session active"
                    : instruction.delivery_mode === "next_run"
                      ? "Prochaine exécution"
                      : "Non renseigné"}
                </dd>
              </div>
              {instruction.last_delivery_error === null ? null : (
                <div>
                  <dt>Dernière erreur de livraison</dt>
                  <dd>{instruction.last_delivery_error}</dd>
                </div>
              )}
              {instruction.cancelled_at === null ? null : (
                <div>
                  <dt>Annulée</dt>
                  <dd>
                    {formatTimestamp(instruction.cancelled_at)}
                    {instruction.cancel_reason === null
                      ? ""
                      : ` · ${instruction.cancel_reason}`}
                  </dd>
                </div>
              )}
            </dl>
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * Human-only journal projection of one session. Loading, error and empty
 * states are explicit and in French; an absent field is shown as absent, not
 * filled with a guess.
 */
function SessionSummary({
  investigationId,
  session,
  durationEnd,
  blocker,
}: {
  investigationId: string;
  session: AgentSessionView;
  durationEnd: string | null;
  blocker: string;
}) {
  const summary = useAgentSessionSummary(investigationId, session.id);
  const error = summary.isError ? toWorkshopApiError(summary.error) : null;
  const data = summary.data;
  return (
    <section className="m24-operator__summary" aria-label="Résumé de session">
      {error !== null ? (
        <p role="alert" className="error-message">
          Résumé de session indisponible — {describeTaskError(error)}
        </p>
      ) : data === undefined ? (
        <p role="status">Chargement des instructions…</p>
      ) : (
        <InstructionBlock window={data.instructions} />
      )}
      <details>
        <summary>Détails techniques</summary>
        <dl className="m24-metadata">
          <div>
            <dt>Identifiant de session</dt>
            <dd>
              <code>{session.id}</code>
            </dd>
          </div>
          <div>
            <dt>Référence de session hôte</dt>
            <dd>{session.host_session_ref ?? "Aucune"}</dd>
          </div>
          <div>
            <dt>Identifiant de tâche</dt>
            <dd>
              <code>{session.task_id}</code>
            </dd>
          </div>
          <div>
            <dt>Bail de tâche</dt>
            <dd>{session.task_lease_id ?? "Aucun"}</dd>
          </div>
          <div>
            <dt>Fence</dt>
            <dd>{session.task_lease_fence ?? "Aucun"}</dd>
          </div>
          <div>
            <dt>Digest des capacités</dt>
            <dd>
              <code>{session.capability_sha256}</code>
            </dd>
          </div>
          <div>
            <dt>Passation</dt>
            <dd>{session.handoff_id ?? "Aucune"}</dd>
          </div>
          <div>
            <dt>Checkpoint</dt>
            <dd>{session.checkpoint_ref ?? "Aucun"}</dd>
          </div>
          <div>
            <dt>Digest du checkpoint</dt>
            <dd>
              {session.checkpoint_sha256 === null ? (
                "Aucun"
              ) : (
                <code>{session.checkpoint_sha256}</code>
              )}
            </dd>
          </div>
          <div>
            <dt>Durée de session</dt>
            <dd>{formatDuration(session.opened_at, durationEnd)}</dd>
          </div>
          <div>
            <dt>Blocage ou motif d’arrêt</dt>
            <dd>{blocker}</dd>
          </div>
        </dl>
        {error !== null ? (
          <p className="m24-muted">
            Les journaux techniques sont indisponibles pour cette session.
          </p>
        ) : data === undefined ? null : (
          <>
            <h6>Résumé dérivé du journal</h6>
            <OperationList window={data.operations} />
            <BudgetBlock budget={data.budget} />
            <BrokerBlock broker={data.broker} />
            <HandoffBlock handoff={data.handoff} />
            <InstructionTechnicalDetails window={data.instructions} />
          </>
        )}
      </details>
    </section>
  );
}

function SessionRow({
  investigationId,
  session,
  task,
  onViewResults,
  onTransfer,
}: {
  investigationId: string;
  session: AgentSessionView;
  task: TaskView | undefined;
  onViewResults?: () => void;
  onTransfer?: () => void;
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
  const lastResult =
    task === undefined
      ? "Tâche inconnue"
      : task.result_refs.length === 0
        ? "Aucun résultat soumis"
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
          const outcome =
            result.state === "acknowledged"
              ? "Instruction acquittée par la session."
              : result.state === "delivered"
                ? "Instruction transmise à la session."
                : result.state === "queued"
                  ? `Instruction en file persistante (position ${result.queue_position}).`
                  : `Instruction : ${INSTRUCTION_JOURNAL_STATE_LABELS[result.state].toLowerCase()}.`;
          finish(outcome);
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
          {ADAPTER_LABELS[session.adapter_kind]} —{" "}
          <span className={sessionBadgeClass(session.state)}>
            {SESSION_STATE_LABELS[session.state]}
          </span>
        </h5>
      </header>
      <p className="m24-muted">
        Tâche : {task === undefined ? "Tâche inconnue" : task.title}
      </p>
      <dl className="m24-metadata">
        <div>
          <dt>Dernière activité</dt>
          <dd>{formatTimestamp(session.last_activity_at)}</dd>
        </div>
        <div>
          <dt>Dernier résultat</dt>
          <dd>{lastResult}</dd>
        </div>
        {task === undefined ? null : (
          <div>
            <dt>État de la tâche</dt>
            <dd>{TASK_STATUS_LABELS[task.status]}</dd>
          </div>
        )}
      </dl>

      <SessionSummary
        investigationId={investigationId}
        session={session}
        durationEnd={durationEnd}
        blocker={blocker}
      />

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
            Instruction à envoyer (mise en file si la session est occupée)
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
          </div>
          {enqueue.isPending ? (
            <p role="status">{INSTRUCTION_SUBMISSION_PENDING_LABEL}</p>
          ) : null}
        </div>
      ) : null}

      <div className="m24-inline-form__buttons">
        {onViewResults === undefined ? null : (
          <button
            type="button"
            className="m24-action m24-action--quiet"
            onClick={onViewResults}
          >
            Voir les sorties
          </button>
        )}
        {onTransfer === undefined ? null : (
          <button
            type="button"
            className="m24-action m24-action--quiet"
            onClick={onTransfer}
          >
            Transférer
          </button>
        )}
        {interruptible ? (
          <details className="m24-more-actions">
            <summary>Interrompre la session</summary>
            <p className="m24-muted">
              Arrêt technique de la session. L’interruption métier de la tâche
              se demande avec l’action « Interrompre la tâche ».
            </p>
            {interruptOpen ? null : (
              <button
                type="button"
                className="m24-action m24-action--danger"
                onClick={() => setInterruptOpen(true)}
              >
                Confirmer l’arrêt technique
              </button>
            )}
            {interruptOpen ? (
              <div className="m24-inline-form">
                <label htmlFor={`${baseId}-reason`}>
                  Motif d’arrêt de la session (obligatoire)
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
                    Confirmer l’arrêt de la session
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
          </details>
        ) : null}
      </div>

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
          <h6>Journal borné (20 derniers événements de tâche, non probant)</h6>
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
  taskId,
  task: selectedTask,
  sessionContext,
  onViewResults,
  onTransfer,
}: {
  investigationId: string;
  taskId?: string;
  task?: TaskView;
  sessionContext?: readonly AgentSessionView[];
  onViewResults?: () => void;
  onTransfer?: () => void;
}) {
  const sessions = useAgentSessions(investigationId, undefined, {
    enabled: sessionContext === undefined,
  });
  const tasks = useTasks(investigationId, {
    enabled: selectedTask === undefined,
  });
  // A successful opening removes the task from "sans session" and mounts the
  // session row, so the feedback is kept here, outside that disappearing list.
  const [openFeedback, setOpenFeedback] = useState<OpenSessionFeedback | null>(
    null,
  );
  const taskItems =
    selectedTask === undefined ? (tasks.data?.items ?? []) : [selectedTask];
  const taskById = new Map<string, TaskView>(
    taskItems.map((task) => [task.id, task]),
  );
  const allItems = sessionContext ?? sessions.data ?? [];
  const items =
    taskId === undefined
      ? allItems
      : allItems.filter((session) => session.task_id === taskId);
  const active = items.filter((session) =>
    ACTIVE_SESSION_STATES.has(session.state),
  );
  const resumable = items.filter((session) =>
    RESUMABLE_SESSION_STATES.has(session.state),
  );
  // Assigned operator tasks whose handoff exists but which have no active or
  // resumable session: the explicit session opening belongs here as well as on
  // the task card.
  const scopedTasks = taskItems.filter(
    (task) => taskId === undefined || task.id === taskId,
  );
  const openable = scopedTasks
    .map((task) => ({
      task,
      target:
        sessionForTask(items, task.id) === null
          ? sessionLaunchTarget(task)
          : null,
    }))
    .filter(
      (entry): entry is { task: TaskView; target: SessionLaunchTarget } =>
        entry.target !== null,
    );
  const sessionError =
    sessionContext === undefined && sessions.isError
      ? toWorkshopApiError(sessions.error)
      : null;

  return (
    <section className="m24-card" aria-labelledby="m24-operators-heading">
      <h3 id="m24-operators-heading">
        {taskId === undefined
          ? "Opérateurs actifs"
          : "Contexte agent de la tâche"}
      </h3>
      <p className="m24-muted">
        {taskId === undefined
          ? "Une session Codex CLI ou Claude Code par tâche. Les capacités affichées (reprise, checkpoint) sont celles que le serveur expose ; rien n’est déduit d’un adaptateur présumé."
          : "Fournisseur, état de session, dernier résultat de la tâche et instructions. Les sorties et détails techniques restent accessibles."}
      </p>

      {openFeedback?.kind === "success" ? (
        <p role="status" className="m24-message">
          {openFeedback.text}
        </p>
      ) : null}
      {openFeedback?.kind === "error" ? (
        <p role="alert" className="error-message">
          {openFeedback.text}
        </p>
      ) : null}

      {openable.length === 0 ? null : (
        <section aria-labelledby="m24-operators-pending">
          <h4 id="m24-operators-pending">
            Tâches attribuées sans session ({openable.length})
          </h4>
          <p className="m24-muted">
            L’attribution crée une passation, pas une session. Ici, une session
            est proposée uniquement lorsque le sondage réel des capacités
            atteste le confinement isolé du CLI.
          </p>
          <ul className="m24-operator-list">
            {openable.map(({ task, target }) => (
              <li className="m24-operator" key={task.id}>
                <header className="m24-operator__header">
                  <h5>
                    {ADAPTER_LABELS[target.adapterKind]} · {task.title}
                  </h5>
                  <span className="m24-badge">Attribuée</span>
                </header>
                <OpenSessionAction
                  investigationId={investigationId}
                  target={target}
                  onFeedback={(feedback) =>
                    setOpenFeedback({
                      ...feedback,
                      text: `${task.title} — ${feedback.text}`,
                    })
                  }
                />
              </li>
            ))}
          </ul>
        </section>
      )}

      {sessionContext === undefined && sessions.isPending ? (
        <p role="status">Chargement des sessions…</p>
      ) : sessionError !== null ? (
        <p role="alert" className="error-message">
          {sessionError.code === "agent_sessions_unavailable"
            ? "Agent absent : les sessions d’opérateurs sont indisponibles (503)."
            : `Sessions indisponibles : ${describeTaskError(sessionError)}`}
        </p>
      ) : active.length === 0 ? (
        <p className="empty-state">
          {taskId === undefined
            ? "Aucun opérateur actif."
            : "Aucune session active pour cette tâche."}
        </p>
      ) : (
        <ul className="m24-operator-list">
          {active.map((session) => (
            <SessionRow
              key={session.id}
              investigationId={investigationId}
              session={session}
              task={taskById.get(session.task_id)}
              onViewResults={onViewResults}
              onTransfer={onTransfer}
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
                onViewResults={onViewResults}
                onTransfer={onTransfer}
              />
            ))}
          </ul>
        </section>
      )}
    </section>
  );
}
