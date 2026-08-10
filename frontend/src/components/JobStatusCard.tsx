import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import {
  fetchJob,
  type JobStatus,
  type JobView,
  terminalJobStatuses,
} from "../api/jobs";

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

export function JobStatusCard({ jobId }: { jobId: string }) {
  const job = useJobTracking(jobId);

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
      {job.data.error_message ? (
        <p role="alert" className="error-message">
          {job.data.error_message}
          {job.data.error_code ? ` (${job.data.error_code})` : ""}
        </p>
      ) : null}
    </article>
  );
}
