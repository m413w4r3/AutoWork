/**
 * Bridge plan panel of the M24 cockpit.
 *
 * The request is a human advisor consultation with the working-state CAS and
 * an `Idempotency-Key`. Accepting a version writes the human `plan_accept`
 * decision and materialises its leads as tasks exactly once; a later version
 * only adds new task keys and never rewrites existing tasks. Unparsed output
 * stays an advisory answer and is labelled "avis non probant".
 */

import { useEffect, useId, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  investigationQueryKeys,
  useInvestigation,
} from "../../../api/investigation";
import {
  createIdempotencyKey,
  toWorkshopApiError,
  type WorkshopApiError,
} from "../api/client";
import {
  useAcceptPlan,
  useConsultationLedger,
  useMaterializePlan,
  usePlan,
  usePlans,
  useRequestPlan,
  type InvestigationLeadV1,
  type PlanParseStatus,
  type PlanRequestInput,
} from "../api/plans";
import { useTasks } from "../api/tasks";
import { CONSULTATION_LABELS } from "../opinion/labels";
import { WorkshopLongOperationState } from "../shell/WorkshopStates";
import { INVOCATION_STATUS_LABELS, labelOf } from "../samples/samplesLabels";
import {
  TASK_STATUS_LABELS,
  formatTimestamp,
  listOrPlaceholder,
  orPlaceholder,
  shortSha,
} from "../tasks/labels";

const RECONCILIATION_STATE_LABELS: Record<string, string> = {
  not_required: "Non requise",
  required: "Requise",
  recorded: "Enregistrée",
  terminal: "Terminale",
};

type LeadFieldKey = Exclude<keyof InvestigationLeadV1, "lead_id">;

const LEAD_FIELDS: ReadonlyArray<{
  key: LeadFieldKey;
  label: string;
  kind: "text" | "list";
}> = [
  { key: "objective", label: "Objectif", kind: "text" },
  { key: "cti_justification", label: "Justification CTI", kind: "text" },
  { key: "claim_refs", label: "Références de claims", kind: "list" },
  { key: "start_evidence_refs", label: "Preuves de départ", kind: "list" },
  { key: "sample_refs", label: "Échantillons", kind: "list" },
  { key: "tools", label: "Outils", kind: "list" },
  { key: "preconditions", label: "Préconditions", kind: "list" },
  { key: "interest", label: "Intérêt", kind: "text" },
  { key: "conclusion_criteria", label: "Critères de conclusion", kind: "text" },
  { key: "limits", label: "Limites", kind: "text" },
  { key: "depends_on", label: "Dépendances", kind: "list" },
];

function parseStatusLabel(status: PlanParseStatus): string {
  switch (status) {
    case "parsed":
      return "Parsé";
    case "partially_parsed":
      return "Partiellement parsé";
    case "unparsed":
      return "Non parsé — avis seulement";
  }
}

function leadFieldValue(
  lead: InvestigationLeadV1,
  field: (typeof LEAD_FIELDS)[number],
): string {
  const raw: string | string[] = lead[field.key];
  if (field.kind === "list") {
    return Array.isArray(raw) ? listOrPlaceholder(raw) : "Non renseigné";
  }
  return typeof raw === "string" ? orPlaceholder(raw) : "Non renseigné";
}

function describePlanError(error: WorkshopApiError): string {
  switch (error.code) {
    case "external_disclosure_denied":
      return "G2 refusé : la divulgation externe n’est pas autorisée pour cette enquête.";
    case "bridge_checkpoint_pending":
      return "Bridge n’a pas encore traité son point de contrôle ; réessayez plus tard.";
    case "operator_mode_required":
      return "L’enquête n’est pas en mode opérateur ; la demande Bridge est indisponible.";
    case "stale_working_state":
      return "L’état de l’enquête a changé (version obsolète). Les données sont rechargées ; réessayez.";
    case "investigation_not_started":
      return "L’enquête n’a pas démarré : aucun plan ne peut être demandé.";
    case "network_error":
      return "Bridge est indisponible (échec réseau).";
    case "plan_unparsed":
      return "La réponse Bridge n’a pas pu être parsée : elle reste un avis, jamais une preuve.";
    case "plan_dependency_unresolved":
      return "Une dépendance du plan ne peut pas être résolue ; aucune tâche n’a été créée.";
    case "plan_dependency_cycle":
      return "Le plan contient un cycle de dépendances ; aucune tâche n’a été créée.";
    case "stale_plan_version":
      return "Cette version du plan a changé ; rechargez avant d’accepter.";
    case "plan_materialization_conflict":
      return "La matérialisation entre en conflit avec l’état courant ; rechargez les tâches.";
    default:
      return `${error.code} (HTTP ${error.status})`;
  }
}

export function PlanPanel({ investigationId }: { investigationId: string }) {
  const baseId = useId();
  const queryClient = useQueryClient();
  const investigation = useInvestigation(investigationId);
  const plans = usePlans(investigationId);
  const tasks = useTasks(investigationId);
  const consultations = useConsultationLedger(investigationId);
  const requestMutation = useRequestPlan(investigationId);
  const materialize = useMaterializePlan(investigationId);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [confirmAccept, setConfirmAccept] = useState(false);
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [contextTaskId, setContextTaskId] = useState(
    () =>
      new URL(window.location.href).searchParams.get("context_task_id") ?? "",
  );
  const [unknownState, setUnknownState] = useState(false);
  const frozenRequest = useRef<{
    formSignature: string;
    input: PlanRequestInput;
    idempotencyKey: string;
  } | null>(null);

  const currentPlan =
    plans.data?.plans.find((summary) => summary.current) ?? null;
  const effectiveId = selectedId ?? currentPlan?.plan_version_id ?? "";
  const plan = usePlan(investigationId, effectiveId);
  const accept = useAcceptPlan(investigationId, effectiveId);
  const lastRequest = requestMutation.data ?? null;
  const acceptResult = accept.data ?? null;
  const detail = plan.data;
  const linkedTasks =
    detail === undefined
      ? []
      : (tasks.data?.items ?? []).filter(
          (task) => task.plan_version_id === detail.plan_version_id,
        );
  const canAccept =
    detail !== undefined &&
    detail.current &&
    !tasks.isPending &&
    linkedTasks.length === 0;
  const acceptDisabled = !canAccept || !confirmAccept || accept.isPending;

  // A `context_task_id` query parameter comes from the task card link. Drop it
  // once the task list proves the id unknown instead of sending a 404.
  useEffect(() => {
    if (contextTaskId === "" || tasks.data === undefined) return;
    if (!tasks.data.items.some((task) => task.id === contextTaskId)) {
      setContextTaskId("");
    }
  }, [contextTaskId, tasks.data]);

  useEffect(() => {
    if (tasks.isError) setContextTaskId("");
  }, [tasks.isError]);

  // The task card link navigates with `popstate` while this view may already
  // be mounted; the URL parameter stays the source of truth for the link.
  useEffect(() => {
    const handlePopState = () => {
      setContextTaskId(
        new URL(window.location.href).searchParams.get("context_task_id") ?? "",
      );
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  function fail(error: unknown) {
    const apiError = toWorkshopApiError(error);
    setMessage(null);
    setErrorMessage(describePlanError(apiError));
    if (apiError.code === "stale_working_state") {
      void queryClient.invalidateQueries({
        queryKey: investigationQueryKeys.detail(investigationId),
      });
    }
  }

  function handleRequestError(error: unknown) {
    const apiError = toWorkshopApiError(error);
    fail(apiError);
    if (apiError.code === "network_error" || apiError.status === 0) {
      // The plan may have been persisted before the transport failed: keep the
      // frozen body/key and let the human check the registry.
      setUnknownState(true);
      return;
    }
    // A typed refusal proves no version was written; the next attempt re-reads
    // the working state and mints a new key.
    setUnknownState(false);
    frozenRequest.current = null;
  }

  function refreshRegistry() {
    // A refreshed ledger cannot prove the row is this request's: keep the
    // unknown state until a submission succeeds.
    void consultations.refetch();
  }

  function submitPlanRequest() {
    const version = investigation.data?.version;
    if (version === undefined) {
      setErrorMessage("La version de l’état de l’enquête est indisponible.");
      return;
    }
    const trimmed = question.trim();
    // One key per frozen request body: an unchanged form replays the exact
    // same operation after a network timeout instead of minting a new one.
    const formSignature = JSON.stringify([trimmed, contextTaskId]);
    let submission = frozenRequest.current;
    if (submission === null || submission.formSignature !== formSignature) {
      submission = {
        formSignature,
        input: {
          expected_version: version,
          ...(trimmed === "" ? {} : { question: trimmed }),
          ...(contextTaskId === "" ? {} : { context_task_id: contextTaskId }),
        },
        idempotencyKey: createIdempotencyKey(),
      };
      frozenRequest.current = submission;
    }
    setMessage(null);
    setErrorMessage(null);
    requestMutation.mutate(
      { input: submission.input, idempotencyKey: submission.idempotencyKey },
      {
        onSuccess: (response) => {
          accept.reset();
          // The request is settled: the next explicit click is a new request
          // and must mint a new key under the refreshed working state.
          frozenRequest.current = null;
          setUnknownState(false);
          if (response.plan_version_id !== null) {
            setSelectedId(response.plan_version_id);
          }
          const requestedTaskId = submission.input.context_task_id;
          const requestedTask =
            requestedTaskId === undefined
              ? undefined
              : (tasks.data?.items ?? []).find(
                  (task) => task.id === requestedTaskId,
                );
          const contextSuffix =
            requestedTask === undefined
              ? ""
              : ` (tâche de contexte : ${requestedTask.task_key})`;
          if (response.invocation_status === "unavailable") {
            setMessage(
              "Bridge est indisponible : aucune version de plan n’a été créée.",
            );
          } else if (response.advice_only) {
            setMessage(
              `Bridge a répondu par un avis non parsé : il reste consultatif (avis non probant)${contextSuffix}.`,
            );
          } else if (response.plan_version_id !== null) {
            setMessage(
              `Version ${response.plan_version ?? ""} du plan enregistrée (${parseStatusLabel(response.plan_parse_status)})${contextSuffix}.`,
            );
          } else {
            setMessage(
              `Réponse Bridge reçue ; la version du plan peut être matérialisée${contextSuffix}.`,
            );
          }
          void queryClient.invalidateQueries({
            queryKey: investigationQueryKeys.detail(investigationId),
          });
        },
        onError: handleRequestError,
      },
    );
  }

  function materializeResponse() {
    if (lastRequest === null) return;
    setMessage(null);
    setErrorMessage(null);
    materialize.mutate(
      { source_invocation_id: lastRequest.invocation_id },
      {
        onSuccess: (response) => {
          setSelectedId(response.plan_version_id);
          setMessage(
            `Version ${response.plan_version} matérialisée (${response.lead_count} piste(s), ${parseStatusLabel(response.parse_status)}).`,
          );
        },
        onError: fail,
      },
    );
  }

  function acceptPlanVersion() {
    if (detail === undefined || !canAccept) return;
    const trimmed = reason.trim();
    setMessage(null);
    setErrorMessage(null);
    accept.mutate(
      {
        expected_sha256: detail.content_sha256,
        ...(trimmed === "" ? {} : { reason: trimmed }),
      },
      {
        onSuccess: (response) => {
          setConfirmAccept(false);
          setReason("");
          const created = response.tasks.filter((task) => task.created).length;
          const existing = response.tasks.length - created;
          setMessage(
            `${response.idempotent_replay ? "Acceptation rejouée (idempotente)" : "Plan accepté"} : ${created} tâche(s) créée(s), ${existing} déjà présente(s).`,
          );
        },
        onError: fail,
      },
    );
  }

  return (
    <section className="m24-card" aria-labelledby="m24-plan-heading">
      <h3 id="m24-plan-heading">Plan Bridge</h3>
      <p className="m24-muted">
        Bridge produit le plan à partir du contexte autorisé de l’enquête.
        L’acceptation est une décision humaine qui crée les tâches une seule
        fois ; une version ultérieure n’en réécrit ni n’en annule aucune.
      </p>

      <div className="m24-inline-form">
        <label htmlFor={`${baseId}-question`}>
          Question facultative pour Bridge
        </label>
        <textarea
          id={`${baseId}-question`}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          rows={2}
          placeholder="Priorités, doutes, pistes à couvrir…"
        />
        <label htmlFor={`${baseId}-context-task`}>
          Tâche de contexte (facultative)
        </label>
        <select
          id={`${baseId}-context-task`}
          value={contextTaskId}
          onChange={(event) => setContextTaskId(event.target.value)}
          disabled={tasks.isPending}
        >
          <option value="">Aucune — demande générale</option>
          {(tasks.data?.items ?? []).map((task) => (
            <option key={task.id} value={task.id}>
              {task.task_key} · {task.title}
            </option>
          ))}
        </select>
        <p className="m24-muted">
          La tâche choisie est transmise comme contexte borné (références et
          hash, jamais comme preuve) et lie l’invocation à cette tâche. Une
          révision de plan ne change ni son statut ni son résultat.
        </p>
        <p className="m24-muted">
          Version de l’état utilisée pour le CAS :{" "}
          {investigation.data?.version ?? "indisponible"}. La demande porte une
          clé d’idempotence, conservée tant que ce formulaire reste identique.
        </p>
        <div className="m24-inline-form__buttons">
          <button
            type="button"
            className="m24-action"
            onClick={submitPlanRequest}
            disabled={
              investigation.data === undefined || requestMutation.isPending
            }
          >
            {requestMutation.isPending
              ? "Demande en cours auprès de Bridge…"
              : unknownState
                ? "Réessayer avec la même clé"
                : "Demander un plan à Bridge"}
          </button>
        </div>
        {requestMutation.isPending ? (
          <WorkshopLongOperationState
            label="Demande de plan à Bridge en cours"
            detail="Opération longue : Bridge peut mettre plusieurs minutes à répondre ; la demande reste soumise à G2 et à la divulgation externe."
          />
        ) : null}
      </div>

      {unknownState ? (
        <div className="m24-advisory" role="alert">
          <p>
            <strong>État inconnu.</strong> La demande précédente a peut-être été
            reçue avant l’échec réseau. Vérifiez le registre avant de réessayer
            ; un nouvel envoi du même formulaire réutilise la même clé
            d’idempotence.
          </p>
          <div className="m24-inline-form__buttons">
            <button
              type="button"
              className="m24-action m24-action--quiet"
              onClick={refreshRegistry}
              disabled={consultations.isFetching}
            >
              Vérifier le registre
            </button>
          </div>
        </div>
      ) : null}

      {investigation.isError ? (
        <p role="alert" className="error-message">
          Version de l’état de l’enquête indisponible :{" "}
          {toWorkshopApiError(investigation.error).code}. La demande de plan
          reste désactivée.
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

      {lastRequest === null ? null : (
        <div className="m24-advisory">
          <p>
            <strong>Réponse de la demande :</strong>{" "}
            {parseStatusLabel(lastRequest.plan_parse_status)} · Bridge :{" "}
            {parseStatusLabel(lastRequest.advisor_parse_status)}
            {lastRequest.idempotent_replay ? " · rejeu idempotent" : ""}
          </p>
          {lastRequest.missing_fields.length === 0 ? null : (
            <p>
              Champs manquants dans la réponse :{" "}
              {lastRequest.missing_fields.join(", ")}.
            </p>
          )}
          {lastRequest.advice_only ||
          lastRequest.plan_parse_status === "unparsed" ? (
            <p className="m24-badge m24-badge--advisory">
              Avis Bridge — avis non probant. Aucune version de plan n’a été
              créée.
            </p>
          ) : null}
          {lastRequest.plan_version_id === null &&
          !lastRequest.advice_only &&
          lastRequest.invocation_status !== "unavailable" ? (
            <button
              type="button"
              className="m24-action"
              onClick={materializeResponse}
              disabled={materialize.isPending}
            >
              Matérialiser la version du plan
            </button>
          ) : null}
        </div>
      )}

      {plans.isPending ? (
        <p role="status">Chargement des plans…</p>
      ) : plans.isError ? (
        <p role="alert" className="error-message">
          Les plans ne peuvent pas être chargés :{" "}
          {toWorkshopApiError(plans.error).code}.
        </p>
      ) : plans.data.plans.length === 0 ? (
        <p className="empty-state">
          Aucun plan pour cette enquête. Demandez un premier plan à Bridge.
        </p>
      ) : (
        <ul className="m24-plan-versions">
          {[...plans.data.plans]
            .sort((left, right) => right.version - left.version)
            .map((summary) => (
              <li key={summary.plan_version_id}>
                <button
                  type="button"
                  className={
                    summary.plan_version_id === effectiveId
                      ? "m24-version m24-version--selected"
                      : "m24-version"
                  }
                  aria-pressed={summary.plan_version_id === effectiveId}
                  onClick={() => {
                    setSelectedId(summary.plan_version_id);
                    accept.reset();
                    setConfirmAccept(false);
                  }}
                >
                  Version {summary.version} ·{" "}
                  {parseStatusLabel(summary.parse_status)} ·{" "}
                  {summary.lead_count} piste(s)
                </button>
                {summary.current ? (
                  <span className="m24-badge">Courante</span>
                ) : (
                  <span className="m24-badge m24-badge--muted">
                    Version antérieure
                  </span>
                )}
                <p className="m24-muted">
                  SHA-256 : <code>{shortSha(summary.content_sha256)}</code> ·
                  créée le {formatTimestamp(summary.created_at)}
                </p>
              </li>
            ))}
        </ul>
      )}

      {effectiveId === "" ? null : plan.isPending ? (
        <p role="status">Chargement de la version du plan…</p>
      ) : plan.isError ? (
        <p role="alert" className="error-message">
          La version du plan ne peut pas être chargée :{" "}
          {toWorkshopApiError(plan.error).code}.
        </p>
      ) : detail === undefined ? null : (
        <div className="m24-plan-detail">
          <h4>
            Version {detail.version} — {parseStatusLabel(detail.parse_status)}
          </h4>
          <p className="m24-muted">
            SHA-256 : <code>{detail.content_sha256}</code> · source{" "}
            <code>{detail.source_invocation_id}</code>
          </p>
          {detail.parse_status === "partially_parsed" ? (
            <p className="m24-warning">
              Plan partiellement parsé : les champs vides sont affichés « Non
              renseigné » et restent consultatifs.
            </p>
          ) : null}
          {detail.content.leads.length === 0 ? (
            <p className="empty-state">
              Aucune piste parsée dans cette version.
            </p>
          ) : (
            <ol className="m24-leads">
              {detail.content.leads.map((lead, index) => (
                <li key={`${detail.plan_version_id}-lead-${index}`}>
                  <article className="m24-lead">
                    <h5>
                      Piste {index + 1} — {orPlaceholder(lead.title)}
                    </h5>
                    {lead.lead_id === undefined ? null : (
                      <p className="m24-muted">
                        Identifiant de piste : <code>{lead.lead_id}</code>
                      </p>
                    )}
                    <dl className="m24-metadata">
                      {LEAD_FIELDS.map((field) => (
                        <div key={field.key}>
                          <dt>{field.label}</dt>
                          <dd>{leadFieldValue(lead, field)}</dd>
                        </div>
                      ))}
                    </dl>
                  </article>
                </li>
              ))}
            </ol>
          )}

          {linkedTasks.length > 0 ? (
            <p className="m24-muted">
              Tâches déjà matérialisées depuis cette version :{" "}
              {linkedTasks.length}. L’acceptation n’est plus proposée ; les
              versions ultérieures ne les réécrivent pas.
            </p>
          ) : !detail.current ? (
            <p className="m24-muted">
              Version antérieure : lecture seule. Seule la version courante peut
              être acceptée depuis cette vue.
            </p>
          ) : (
            <div className="m24-inline-form">
              <label htmlFor={`${baseId}-accept-reason`}>
                Motif d’acceptation (facultatif)
              </label>
              <textarea
                id={`${baseId}-accept-reason`}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                rows={2}
              />
              <label className="m24-confirm">
                <input
                  type="checkbox"
                  checked={confirmAccept}
                  onChange={(event) => setConfirmAccept(event.target.checked)}
                />
                Je confirme l’acceptation de cette version (décision humaine ;
                les tâches ne sont créées qu’une fois).
              </label>
              <div className="m24-inline-form__buttons">
                <button
                  type="button"
                  className="m24-action"
                  onClick={acceptPlanVersion}
                  disabled={acceptDisabled}
                >
                  {accept.isPending
                    ? "Acceptation en cours…"
                    : "Accepter le plan et créer les tâches"}
                </button>
              </div>
            </div>
          )}

          {acceptResult === null ? null : (
            <div role="status" className="m24-message">
              <ul>
                {acceptResult.tasks.map((task) => (
                  <li key={task.task_id}>
                    {task.title} — {labelOf(TASK_STATUS_LABELS, task.status)}
                    {task.created ? " (créée)" : " (déjà présente)"}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      <details className="m24-ledger">
        <summary>
          Registre des consultations Bridge (
          {consultations.data?.consultations.length ?? 0})
        </summary>
        {consultations.isPending ? (
          <p role="status">Chargement du registre…</p>
        ) : consultations.isError ? (
          <p role="alert" className="error-message">
            Registre indisponible :{" "}
            {toWorkshopApiError(consultations.error).code}.
          </p>
        ) : consultations.data.consultations.length === 0 ? (
          <p className="empty-state">Aucune consultation Bridge enregistrée.</p>
        ) : (
          <ul className="m24-consultations">
            {consultations.data.consultations.map((consultation) => (
              <li key={consultation.invocation_id}>
                <p>
                  <strong>
                    {labelOf(
                      CONSULTATION_LABELS,
                      consultation.consultation_type,
                    )}
                  </strong>{" "}
                  · {INVOCATION_STATUS_LABELS[consultation.invocation_status]} ·{" "}
                  {parseStatusLabel(consultation.parse_status)} ·{" "}
                  {formatTimestamp(consultation.created_at)}
                </p>
                <p className="m24-muted">
                  {consultation.advisory_only
                    ? "Avis Bridge — non probant"
                    : "Consultation non étiquetée consultative (à vérifier)"}
                  {consultation.usable ? " · texte exploitable" : ""}
                  {consultation.readable ? " · texte lisible" : ""} ·
                  réconciliation :{" "}
                  {labelOf(
                    RECONCILIATION_STATE_LABELS,
                    consultation.reconciliation_state,
                  )}
                </p>
              </li>
            ))}
          </ul>
        )}
      </details>
    </section>
  );
}
