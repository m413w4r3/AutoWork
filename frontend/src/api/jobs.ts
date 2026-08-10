export type JobStatus =
  "queued" | "running" | "waiting_human" | "succeeded" | "failed" | "cancelled";

export interface JobView {
  id: string;
  kind: string;
  aggregate_type: string;
  aggregate_id: string;
  status: JobStatus;
  progress_current: number;
  progress_total: number;
  user_message: string | null;
  attempt: number;
  max_attempts: number;
  next_retry_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  error_code: string | null;
  error_message: string | null;
  correlation_id: string;
  output_reference: string | null;
  cancellation_requested: boolean;
  created_at: string;
  updated_at: string;
}

export const terminalJobStatuses: ReadonlySet<JobStatus> = new Set([
  "succeeded",
  "failed",
  "cancelled",
]);

export async function fetchJob(jobId: string): Promise<JobView> {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
  if (!response.ok) {
    throw new Error(`Job endpoint returned ${response.status}`);
  }
  return (await response.json()) as JobView;
}

export async function cancelJob(jobId: string): Promise<JobView> {
  const response = await fetch(
    `/api/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST" },
  );
  if (!response.ok) {
    throw new Error(`Job cancellation returned ${response.status}`);
  }
  return (await response.json()) as JobView;
}
