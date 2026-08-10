import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import {
  cancelJob,
  fetchJob,
  retryJob,
  type JobStatus,
  type JobView,
  terminalJobStatuses,
} from "../api/jobs";

const bridgeErrors: Record<
  string,
  { message: string; kind: "transient" | "configuration" | "terminal" }
> = {
  bridge_unreachable: {
    message: "Le bridge ChatGPT est inaccessible.",
    kind: "transient",
  },
  bridge_timeout: {
    message: "La recherche ChatGPT a dépassé le délai autorisé.",
    kind: "transient",
  },
  bridge_rate_limited: {
    message: "Le bridge limite temporairement les requêtes.",
    kind: "transient",
  },
  bridge_extension_disconnected: {
    message: "L’extension Chrome est déconnectée.",
    kind: "transient",
  },
  bridge_ui_timeout: {
    message: "L’inspection de l’interface ChatGPT a expiré.",
    kind: "transient",
  },
  bridge_server_error: {
    message: "Le bridge ChatGPT a rencontré une erreur.",
    kind: "transient",
  },
  bridge_auth_failed: {
    message: "L’authentification du bridge est incorrecte.",
    kind: "configuration",
  },
  bridge_payload_conflict: {
    message: "La clé d’idempotence correspond à une autre requête.",
    kind: "terminal",
  },
  bridge_protocol_error: {
    message: "Le bridge a renvoyé une réponse incompatible.",
    kind: "terminal",
  },
};

const statusLabels: Record<JobStatus, string> = {
  queued: "En attente",
  running: "En cours",
  waiting_human: "Validation humaine requise",
  succeeded: "Terminée",
  failed: "Échec",
  cancelled: "Annulée",
};

function useJobTracking(jobId: string) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => fetchJob(jobId),
    refetchInterval: ({ state }) => {
      const job = state.data;
      return job && terminalJobStatuses.has(job.status) ? false : 2_000;
    },
  });

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      return;
    }
    const stream = new EventSource(
      `/api/jobs/${encodeURIComponent(jobId)}/events`,
    );
    const update = (event: MessageEvent<string>) => {
      const job = JSON.parse(event.data) as JobView;
      queryClient.setQueryData(["job", jobId], job);
      if (terminalJobStatuses.has(job.status)) {
        stream.close();
      }
    };
    stream.addEventListener("job", update as EventListener);
    stream.onerror = () => stream.close();
    return () => stream.close();
  }, [jobId, queryClient]);

  return query;
}

export function JobStatusCard({
  jobId,
  onTerminal,
  onRetryStructuring,
}: {
  jobId: string;
  onTerminal?: (status: JobStatus) => void;
  onRetryStructuring?: (researchModelRunId: string) => void;
}) {
  const job = useJobTracking(jobId);
  const queryClient = useQueryClient();
  const cancellation = useMutation({
    mutationFn: () => cancelJob(jobId),
    onSuccess: (updated) => queryClient.setQueryData(["job", jobId], updated),
  });
  const retry = useMutation({
    mutationFn: () => retryJob(jobId),
    onSuccess: (updated) => queryClient.setQueryData(["job", jobId], updated),
  });
  useEffect(() => {
    if (job.data && terminalJobStatuses.has(job.data.status)) {
      onTerminal?.(job.data.status);
    }
  }, [job.data, onTerminal]);

  if (job.isPending) {
    return <p role="status">Chargement de la tâche…</p>;
  }
  if (job.isError) {
    return (
      <p role="alert" className="error-message">
        Impossible de récupérer l’état de la tâche.
      </p>
    );
  }

  const progress =
    job.data.progress_total > 0
      ? Math.round((job.data.progress_current / job.data.progress_total) * 100)
      : 0;
  const bridgeError = job.data.error_code
    ? bridgeErrors[job.data.error_code]
    : undefined;
  const canRetry =
    job.data.status === "failed" &&
    bridgeError?.kind === "transient" &&
    job.data.attempt < job.data.max_attempts;
  const details = job.data.error_details;
  const canRetryStructuring =
    job.data.status === "failed" &&
    details?.can_retry_structuring === true &&
    typeof details.research_model_run_id === "string" &&
    Boolean(onRetryStructuring);

  return (
    <article className={`job-card job-card--${job.data.status}`}>
      <div className="job-card__heading">
        <div>
          <p className="eyebrow">Tâche de fond</p>
          <h2>{statusLabels[job.data.status]}</h2>
        </div>
        <span>
          Tentative {job.data.attempt}/{job.data.max_attempts}
        </span>
      </div>
      <progress
        aria-label="Progression de la tâche"
        max={job.data.progress_total || 1}
        value={job.data.progress_current}
      />
      <p>
        {job.data.progress_total > 0
          ? `${progress} % — ${job.data.progress_current}/${job.data.progress_total}`
          : "Progression en attente"}
      </p>
      {job.data.user_message ? <p>{job.data.user_message}</p> : null}
      {job.data.status === "queued" || job.data.status === "running" ? (
        <button
          className="button button--secondary"
          disabled={cancellation.isPending || job.data.cancellation_requested}
          onClick={() => cancellation.mutate()}
        >
          {job.data.cancellation_requested
            ? "Annulation demandée"
            : "Annuler la tâche"}
        </button>
      ) : null}
      {cancellation.isError ? (
        <p role="alert" className="error-message">
          L’annulation n’a pas pu être demandée.
        </p>
      ) : null}
      {job.data.error_message ? (
        <p role="alert" className="error-message">
          {bridgeError?.message || job.data.error_message}
          {job.data.error_code ? ` (${job.data.error_code})` : ""}
          {bridgeError
            ? ` — ${bridgeError.kind === "configuration" ? "configuration requise" : bridgeError.kind === "transient" ? "erreur transitoire" : "erreur terminale"}`
            : ""}
        </p>
      ) : null}
      {details ? (
        <dl className="job-diagnostics">
          <div>
            <dt>Phase</dt>
            <dd>{details.phase || "inconnue"}</dd>
          </div>
          <div>
            <dt>Validation</dt>
            <dd>{details.validation_kind || "non applicable"}</dd>
          </div>
          <div>
            <dt>Éléments</dt>
            <dd>
              {details.valid_count ?? 0} valides · {details.rejected_count ?? 0}{" "}
              rejetés
            </dd>
          </div>
          {details.model_run_id ? (
            <div>
              <dt>ModelRun</dt>
              <dd>{details.model_run_id}</dd>
            </div>
          ) : null}
          <div>
            <dt>Artefact diagnostic</dt>
            <dd>
              {details.diagnostic_available ? "disponible" : "indisponible"}
            </dd>
          </div>
        </dl>
      ) : null}
      {canRetryStructuring ? (
        <button
          className="button button--secondary"
          onClick={() =>
            onRetryStructuring?.(details.research_model_run_id as string)
          }
        >
          Retenter la structuration
        </button>
      ) : null}
      {canRetry ? (
        <button
          className="button button--secondary"
          disabled={retry.isPending}
          onClick={() => retry.mutate()}
        >
          Réessayer
        </button>
      ) : null}
      {retry.isError ? (
        <p role="alert" className="error-message">
          La relance a échoué.
        </p>
      ) : null}
      <p className="muted">
        Identifiant de diagnostic : {job.data.correlation_id}
      </p>
    </article>
  );
}
