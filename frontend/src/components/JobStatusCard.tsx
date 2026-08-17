import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

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
  bridge_unavailable: {
    message: "Le bridge ChatGPT est inaccessible.",
    kind: "transient",
  },
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
  onUpdate,
  onReprocessReport,
}: {
  jobId: string;
  onTerminal?: (status: JobStatus) => void;
  onUpdate?: (job: JobView) => void;
  onReprocessReport?: (researchModelRunId: string) => void;
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
    if (job.data) onUpdate?.(job.data);
    if (job.data && terminalJobStatuses.has(job.data.status)) {
      onTerminal?.(job.data.status);
    }
  }, [job.data, onTerminal, onUpdate]);
  const [now, setNow] = useState(() => Date.now());
  const active =
    job.data?.status === "queued" || job.data?.status === "running";
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [active]);

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
    Boolean(onReprocessReport);
  const isDiscovery = job.data.kind === "discover_edition";
  const elapsedFrom = job.data.started_at ?? job.data.created_at;
  const elapsedSeconds = Math.max(
    0,
    Math.floor((now - new Date(elapsedFrom).getTime()) / 1_000),
  );

  return (
    <article className={`job-card job-card--${job.data.status}`}>
      <div className="job-card__heading">
        <div>
          <p className="eyebrow">Tâche de fond</p>
          <h2>
            {isDiscovery && active
              ? "ChatGPT recherche et analyse les sources"
              : statusLabels[job.data.status]}
          </h2>
        </div>
        {job.data.max_attempts > 1 ? (
          <span>
            Tentative {job.data.attempt}/{job.data.max_attempts}
          </span>
        ) : null}
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
      {active ? <p>Temps écoulé : {elapsedSeconds} s</p> : null}
      {job.data.user_message ? <p>{job.data.user_message}</p> : null}
      {isDiscovery && details?.phase === "background_bridge_wait" ? (
        <div className="chatgpt-live-status" role="status" aria-live="polite">
          <p>
            <strong>
              {details.chatgpt_phase === "reasoning"
                ? "ChatGPT recherche et réfléchit"
                : details.chatgpt_phase === "generating"
                  ? "ChatGPT génère la réponse"
                  : details.chatgpt_phase === "answering"
                    ? "ChatGPT rédige la réponse"
                    : details.chatgpt_phase === "stabilizing"
                      ? "Réponse reçue — vérification en cours"
                      : details.chatgpt_phase === "waiting_answer"
                        ? "En attente de la réponse ChatGPT"
                        : "ChatGPT travaille"}
            </strong>
          </p>

          {typeof details.chatgpt_output_chars === "number" &&
          details.chatgpt_output_chars > 0 ? (
            <p>
              Réponse visible : {details.chatgpt_output_chars.toLocaleString()}{" "}
              caractères
            </p>
          ) : null}

          {typeof details.chatgpt_stable_for_ms === "number" &&
          details.chatgpt_stable_for_ms > 0 ? (
            <p>
              Inchangée depuis{" "}
              {(details.chatgpt_stable_for_ms / 1000).toFixed(1)} s
            </p>
          ) : null}
        </div>
      ) : null}
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
          {details.phase !== "background_bridge_wait" ? (
            <>
              <div>
                <dt>Validation</dt>
                <dd>{details.validation_kind || "non applicable"}</dd>
              </div>
              <div>
                <dt>Éléments</dt>
                <dd>
                  {details.valid_count ?? 0} valides ·{" "}
                  {details.rejected_count ?? 0} rejetés
                </dd>
              </div>
            </>
          ) : null}
          {details.model_run_id ? (
            <div>
              <dt>ModelRun</dt>
              <dd>{details.model_run_id}</dd>
            </div>
          ) : null}
          {details.bridge_run_id ? (
            <div>
              <dt>Run bridge</dt>
              <dd>{details.bridge_run_id}</dd>
            </div>
          ) : null}
          {details.bridge_state ? (
            <div>
              <dt>État bridge</dt>
              <dd>{details.bridge_state}</dd>
            </div>
          ) : null}
          {details.poll_count !== undefined ? (
            <div>
              <dt>Interrogations</dt>
              <dd>{details.poll_count}</dd>
            </div>
          ) : null}
          {details.elapsed_seconds !== undefined ? (
            <div>
              <dt>Attente bridge</dt>
              <dd>{details.elapsed_seconds} s</dd>
            </div>
          ) : null}
          {details.last_job_heartbeat ? (
            <div>
              <dt>Dernier heartbeat du job</dt>
              <dd>{details.last_job_heartbeat}</dd>
            </div>
          ) : null}
          {details.correlation_id ? (
            <div>
              <dt>Corrélation</dt>
              <dd>{details.correlation_id}</dd>
            </div>
          ) : null}
          {details.phase !== "background_bridge_wait" ? (
            <div>
              <dt>Artefact diagnostic</dt>
              <dd>
                {details.diagnostic_available ? "disponible" : "indisponible"}
              </dd>
            </div>
          ) : null}
        </dl>
      ) : null}
      {canRetryStructuring ? (
        <button
          className="button button--secondary"
          onClick={() =>
            onReprocessReport?.(details.research_model_run_id as string)
          }
        >
          Retraiter le rapport archivé
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
