/**
 * « Demander à Bridge » panel of one M24 task card.
 *
 * A verified human asks Bridge to interpret, critique or propose pivots for
 * this task. The request reuses the task-scoped consultation route (one
 * canonical `advisor.consult`, human requester, `context_task_id`), so it
 * never writes a task status, a task result, a proof or a decision — the panel
 * states that explicitly.
 *
 * No implicit resubmission: one idempotency key is frozen with the exact
 * request body. After a transport failure the panel shows "état inconnu",
 * offers "vérifier le registre", and only replays the same key while the form
 * is unchanged. A typed refusal proves the request did not run, so the next
 * attempt rebuilds its body under the current working-state version.
 */

import { useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { MouseEvent } from "react";

import {
  getInvestigation,
  investigationQueryKeys,
} from "../../../api/investigation";
import { navigate } from "../../../navigation";
import {
  createIdempotencyKey,
  toWorkshopApiError,
  type WorkshopApiError,
} from "../api/client";
import type {
  ConsultationParseStatus,
  ConsultationReconciliationState,
} from "../api/plans";
import type { TaskView } from "../api/tasks";
import {
  deriveTaskConsultationState,
  taskConsultationTypeLabel,
  TASK_CONSULTATION_STATE_LABELS,
  useRequestTaskConsultation,
  useTaskConsultations,
  type TaskConsultationErrorCode,
  type TaskConsultationRequestV1,
  type TaskConsultationType,
} from "../api/taskConsultations";
import type { TaskStatus } from "../api/types";
import { WorkshopLongOperationState } from "../shell/WorkshopStates";
import { formatTimestamp } from "./labels";

const TERMINAL_TASK_STATUSES: ReadonlySet<TaskStatus> = new Set<TaskStatus>([
  "completed",
  "cancelled",
  "failed",
]);

const PARSE_STATUS_LABELS: Record<ConsultationParseStatus, string> = {
  parsed: "Parsée",
  partially_parsed: "Partiellement parsée",
  unparsed: "Non parsée — avis seulement",
};

const RECONCILIATION_LABELS: Record<ConsultationReconciliationState, string> = {
  not_required: "Non requise",
  required: "Requise",
  recorded: "Enregistrée",
  terminal: "Terminale",
};

const CONSULTATION_TYPES: ReadonlyArray<{
  value: TaskConsultationType;
  label: string;
}> = [
  { value: "interpretation", label: "Avis — interpréter la tâche" },
  { value: "critique", label: "Critique technique" },
  { value: "pivot_strategy", label: "Pistes de pivot" },
];

function describeTaskConsultationError(error: WorkshopApiError): string {
  const code = error.code as TaskConsultationErrorCode;
  switch (code) {
    case "task_consultation_task_terminal":
      return "La tâche est terminale : aucune consultation Bridge n’est acceptée. Le registre reste lisible.";
    case "task_consultation_question_required":
      return "La question est vide ; précisez la demande adressée à Bridge.";
    case "task_consultation_anchor_invalid":
      return "L’ancre éditoriale fournie n’est pas une ancre rendue de l’article courant.";
    case "task_consultation_context_invalid":
      return "Le contexte borné de la tâche ne peut pas être assemblé ; aucune demande n’a été envoyée à Bridge.";
    case "task_consultation_not_projected":
      return "La consultation n’apparaît pas au registre après l’appel. Vérifiez le registre avant toute nouvelle demande.";
    case "stale_working_state":
      return "L’état de travail de l’enquête a changé (version obsolète). Les données sont rechargées ; vérifiez l’état puis réessayez.";
    case "bridge_checkpoint_pending":
      return "Bridge n’a pas encore traité son point de contrôle ; réessayez plus tard.";
    case "external_disclosure_denied":
      return "G2 refusé : la divulgation externe n’est pas autorisée pour cette enquête.";
    case "operator_mode_required":
      return "L’enquête n’est pas en mode opérateur ; la demande Bridge est indisponible.";
    case "investigation_not_started":
      return "L’enquête n’a pas démarré : aucune consultation ne peut être demandée.";
    case "operator_payload_invalid_or_sensitive":
      return "La demande transportait un contenu que la politique interdit de divulguer ; elle n’a pas été soumise.";
    case "network_error":
      return "Bridge est indisponible (échec réseau) : l’état de la demande est inconnu.";
    default:
      return `${error.code} (HTTP ${error.status})`;
  }
}

function workshopHref(
  params: Record<string, string>,
  clear: readonly string[] = [],
): string {
  const url = new URL(window.location.href);
  for (const key of clear) url.searchParams.delete(key);
  for (const [key, value] of Object.entries(params)) {
    url.searchParams.set(key, value);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

interface FrozenSubmission {
  formSignature: string;
  input: TaskConsultationRequestV1;
  idempotencyKey: string;
}

export interface TaskBridgePanelProps {
  investigationId: string;
  task: TaskView;
}

export function TaskBridgePanel({
  investigationId,
  task,
}: TaskBridgePanelProps) {
  const baseId = useId();
  const [open, setOpen] = useState(false);
  const [consultationType, setConsultationType] =
    useState<TaskConsultationType>("interpretation");
  const [question, setQuestion] = useState("");
  const [includePrevious, setIncludePrevious] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [unknownState, setUnknownState] = useState(false);
  const frozenSubmission = useRef<FrozenSubmission | null>(null);

  // Same query key as `useInvestigation`; gated on the panel so a closed card
  // sends no request, while the shared cache still deduplicates open panels.
  const investigation = useQuery({
    queryKey: investigationQueryKeys.detail(investigationId),
    queryFn: () => getInvestigation(investigationId),
    enabled: open,
  });
  const consultations = useTaskConsultations(investigationId, task.id, {
    enabled: open,
  });
  const request = useRequestTaskConsultation(investigationId, task.id);

  const version = investigation.data?.version;
  const terminal = TERMINAL_TASK_STATUSES.has(task.status);
  const ledger = [...(consultations.data?.consultations ?? [])].sort(
    (left, right) => right.created_at.localeCompare(left.created_at),
  );
  const busy = request.isPending;

  function submit() {
    const trimmed = question.trim();
    if (trimmed === "") return;
    if (version === undefined) {
      setMessage(null);
      setErrorMessage(
        "La version de l’état de l’enquête est indisponible ; la demande reste désactivée.",
      );
      return;
    }
    // The freezing signature covers every user-editable field; the stored body
    // also pins the working-state version used by the first attempt so a
    // network retry replays the exact same idempotent operation.
    const formSignature = JSON.stringify([
      consultationType,
      trimmed,
      includePrevious,
    ]);
    let submission = frozenSubmission.current;
    if (submission === null || submission.formSignature !== formSignature) {
      submission = {
        formSignature,
        input: {
          consultation_type: consultationType,
          question: trimmed,
          include_previous_consultation: includePrevious,
          expected_working_state_version: version,
        },
        idempotencyKey: createIdempotencyKey(),
      };
      frozenSubmission.current = submission;
    }
    setMessage(null);
    setErrorMessage(null);
    request.mutate(
      { input: submission.input, idempotencyKey: submission.idempotencyKey },
      {
        onSuccess: (response) => {
          const state = deriveTaskConsultationState(response.consultation);
          // The request is settled: a further explicit click is a new
          // consultation and must mint a new key under the new state version.
          frozenSubmission.current = null;
          setUnknownState(false);
          setMessage(
            `Demande : ${TASK_CONSULTATION_STATE_LABELS[state]} — ${PARSE_STATUS_LABELS[response.consultation.parse_status]}.`,
          );
        },
        onError: (error) => {
          const apiError = toWorkshopApiError(error);
          setMessage(null);
          setErrorMessage(describeTaskConsultationError(apiError));
          if (apiError.code === "network_error" || apiError.status === 0) {
            setUnknownState(true);
            return;
          }
          // A typed refusal proves the invocation did not run: forget the
          // frozen body so the next attempt uses the refreshed state version.
          setUnknownState(false);
          frozenSubmission.current = null;
          if (apiError.code === "stale_working_state") {
            void investigation.refetch();
          }
        },
      },
    );
  }

  async function verifyRegistry() {
    const result = await consultations.refetch();
    if (result.isError) return;
    // The registry tells whether a row exists, not whether it is this request:
    // the state stays "unknown" until a submission succeeds.
    setMessage(
      "Registre rafraîchi. Si aucune ligne n’apparaît, la demande n’a probablement pas été reçue : un nouvel envoi du même formulaire réutilise la même clé d’idempotence.",
    );
  }

  function followLink(
    event: MouseEvent<HTMLAnchorElement>,
    params: Record<string, string>,
    clear: readonly string[] = [],
  ) {
    event.preventDefault();
    navigate(workshopHref(params, clear));
  }

  const requestDisabled =
    busy || terminal || version === undefined || question.trim() === "";
  const questionId = `${baseId}-question`;
  const typeId = `${baseId}-type`;
  const registryId = `${baseId}-registry`;
  const linksId = `${baseId}-links`;
  const opinionDraftParams: Record<string, string> = {
    vue: "opinion",
    task_id: task.id,
    bridge_intent: "draft_text",
  };
  const opinionReviewParams: Record<string, string> = {
    ...opinionDraftParams,
    bridge_intent: "review_text",
  };
  const planContextParams: Record<string, string> = {
    vue: "plan",
    context_task_id: task.id,
  };
  // Each destination owns its query contract: drop the other view's params so
  // a pre-filled task does not leak into a later visit.
  const opinionClears = ["context_task_id"];
  const planClears = ["task_id", "bridge_intent"];

  return (
    <section className="m24-task-card__results" aria-label="Demander à Bridge">
      <div className="m24-task-card__actions">
        <button
          type="button"
          className="m24-action m24-action--quiet"
          aria-expanded={open}
          aria-controls={`${baseId}-panel`}
          onClick={() => setOpen((current) => !current)}
        >
          {open ? "Masquer la demande Bridge" : "Demander à Bridge"}
        </button>
      </div>

      {open ? (
        <div id={`${baseId}-panel`}>
          <p className="m24-muted">
            Un avis Bridge reste consultatif : il n’écrit ni résultat de tâche,
            ni preuve, ni décision, et ne change pas le statut de la tâche. La
            demande est soumise à G2/TLP et à la divulgation externe.
          </p>

          <div className="m24-inline-form">
            <label htmlFor={typeId}>Type de demande</label>
            <select
              id={typeId}
              value={consultationType}
              onChange={(event) =>
                setConsultationType(event.target.value as TaskConsultationType)
              }
            >
              {CONSULTATION_TYPES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <label htmlFor={questionId}>Question pour Bridge</label>
            <textarea
              id={questionId}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              rows={3}
              maxLength={8000}
              placeholder="Ce que Bridge doit interpréter, critiquer ou proposer…"
            />
            <label className="m24-confirm">
              <input
                type="checkbox"
                checked={includePrevious}
                onChange={(event) => setIncludePrevious(event.target.checked)}
              />
              Inclure la consultation précédente de cette tâche (référence et
              hash seulement)
            </label>
            <p className="m24-muted">
              Version de l’état utilisée pour le CAS :{" "}
              {version ?? "indisponible"}. La clé d’idempotence est conservée
              tant que ce formulaire reste identique.
            </p>
            {terminal ? (
              <p className="m24-warning">
                Tâche terminale : une consultation n’est plus acceptée ; le
                registre ci-dessous reste lisible.
              </p>
            ) : null}
            {investigation.isError ? (
              <p role="alert" className="error-message">
                Version de l’état de l’enquête indisponible :{" "}
                {toWorkshopApiError(investigation.error).code}. La demande reste
                désactivée.
              </p>
            ) : null}
            <div className="m24-inline-form__buttons">
              <button
                type="button"
                className="m24-action"
                onClick={submit}
                disabled={requestDisabled}
              >
                {busy
                  ? "Demande en cours auprès de Bridge…"
                  : unknownState
                    ? "Réessayer avec la même clé"
                    : "Envoyer la demande à Bridge"}
              </button>
            </div>
            {busy ? (
              <WorkshopLongOperationState
                label="Consultation Bridge en cours"
                detail="Opération longue : Bridge peut mettre plusieurs minutes à répondre. Aucune resoumission automatique n’est effectuée."
              />
            ) : null}
          </div>

          {unknownState ? (
            <div className="m24-advisory" role="alert">
              <p>
                <strong>État inconnu.</strong> La demande précédente a peut-être
                été reçue avant l’échec réseau. Vérifiez le registre avant de
                réessayer ; un nouvel envoi du même formulaire réutilise la même
                clé d’idempotence.
              </p>
              <div className="m24-inline-form__buttons">
                <button
                  type="button"
                  className="m24-action m24-action--quiet"
                  onClick={() => void verifyRegistry()}
                  disabled={consultations.isFetching}
                >
                  Vérifier le registre
                </button>
              </div>
            </div>
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

          <section aria-labelledby={registryId}>
            <h5 id={registryId}>Consultations Bridge de cette tâche</h5>
            <div className="m24-inline-form__buttons">
              <button
                type="button"
                className="m24-action m24-action--quiet"
                onClick={() => void verifyRegistry()}
                disabled={consultations.isFetching}
              >
                {consultations.isFetching
                  ? "Rafraîchissement du registre…"
                  : "Rafraîchir le registre"}
              </button>
            </div>
            {consultations.isPending ? (
              <p role="status">Chargement du registre…</p>
            ) : consultations.isError ? (
              <p role="alert" className="error-message">
                Registre indisponible :{" "}
                {toWorkshopApiError(consultations.error).code}.
              </p>
            ) : ledger.length === 0 ? (
              <p className="empty-state">
                Aucune consultation Bridge enregistrée pour cette tâche.
              </p>
            ) : (
              <ul className="m24-consultations">
                {ledger.map((row) => {
                  const state = deriveTaskConsultationState(row);
                  return (
                    <li key={row.invocation_id}>
                      <p>
                        <strong>
                          {taskConsultationTypeLabel(row.consultation_type)}
                        </strong>{" "}
                        · {TASK_CONSULTATION_STATE_LABELS[state]} ·{" "}
                        {formatTimestamp(row.created_at)}
                      </p>
                      <p className="m24-muted">
                        Analyse : {PARSE_STATUS_LABELS[row.parse_status]} ·
                        réconciliation :{" "}
                        {RECONCILIATION_LABELS[row.reconciliation_state]} ·{" "}
                        {row.advisory_only ? "avis — non preuve" : "à vérifier"}
                        {row.usable ? " · texte exploitable" : ""}
                        {row.readable ? " · texte lisible" : ""} · invocation{" "}
                        <code title={row.invocation_id}>
                          {row.invocation_id.slice(0, 8)}…
                        </code>
                      </p>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section aria-labelledby={linksId}>
            <h5 id={linksId}>Rédaction, correction et plan</h5>
            <p className="m24-muted">
              Ces actions ouvrent la vue existante avec cette tâche pré-remplie
              ; elles ne modifient ni l’avis ni le plan depuis cette carte.
            </p>
            <ul className="m24-subtasks">
              <li>
                <a
                  href={workshopHref(opinionDraftParams, opinionClears)}
                  onClick={(event) =>
                    followLink(event, opinionDraftParams, opinionClears)
                  }
                >
                  Rédiger une Note avec Bridge
                </a>
              </li>
              <li>
                <a
                  href={workshopHref(opinionReviewParams, opinionClears)}
                  onClick={(event) =>
                    followLink(event, opinionReviewParams, opinionClears)
                  }
                >
                  Corriger une Note avec Bridge
                </a>
              </li>
              <li>
                <a
                  href={workshopHref(planContextParams, planClears)}
                  onClick={(event) =>
                    followLink(event, planContextParams, planClears)
                  }
                >
                  Réviser le plan Bridge avec cette tâche
                </a>
              </li>
            </ul>
          </section>
        </div>
      ) : null}
    </section>
  );
}
