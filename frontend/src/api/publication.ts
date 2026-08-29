import { ApiError } from "./editions";

export type PublicationDecision = "include" | "exclude";

export type ReviewRunStatus =
  "queued" | "running" | "ready" | "needs_review" | "failed" | "cancelled";

export type ReviewRetryStage =
  | "sources"
  | "references"
  | "extraction"
  | "synthesis"
  | "analyst_research"
  | "analyst_note"
  | "assembly";

export interface ReviewItem {
  position: number;
  subject_id: string;
  title: string;
  run_id: string;
  pipeline_generation: number;
  run_status: ReviewRunStatus;
  document_artifact_id: string | null;
  document_artifact_version: number | null;
  document_input_hash: string | null;
  effective_decision_id: string | null;
  effective_decision: PublicationDecision | null;
  included: boolean;
  blocking: boolean;
  can_retry: boolean;
  retry_stage: ReviewRetryStage | null;
  error_code: string | null;
  error_message: string | null;
}

export type EditionReviewItem = ReviewItem;

export interface EditionReview {
  edition_id: string;
  items: ReviewItem[];
  can_accept: boolean;
}

export interface ReviewDecision {
  id: string;
  edition_id: string;
  subject_id: string;
  production_run_id: string;
  pipeline_generation: number;
  document_artifact_id: string | null;
  document_artifact_version: number | null;
  document_input_hash: string | null;
  decision: PublicationDecision;
  actor_id: string;
  reason: string | null;
  occurred_at: string;
}

export interface RetryProductionRunResponse {
  run_id: string;
  status: ReviewRunStatus;
  job_id: string | null;
  pipeline_generation: number;
}

interface ReviewDocumentIdentity {
  document_artifact_id: string;
  document_artifact_version: number;
  document_input_hash: string;
}

function documentIdentity(item: ReviewItem): ReviewDocumentIdentity {
  if (
    item.document_artifact_id === null ||
    item.document_artifact_version === null ||
    item.document_input_hash === null
  ) {
    throw new Error("Aucun document publiable n’est disponible.");
  }
  return {
    document_artifact_id: item.document_artifact_id,
    document_artifact_version: item.document_artifact_version,
    document_input_hash: item.document_input_hash,
  };
}

export async function getEditionReview(
  editionId: string,
): Promise<EditionReview> {
  return request(`/api/editions/${editionId}/review`);
}

export async function includeReviewItem(
  editionId: string,
  item: ReviewItem,
  reason?: string,
): Promise<ReviewDecision> {
  const identity = documentIdentity(item);
  const normalizedReason = reason?.trim();
  return request(
    `/api/editions/${editionId}/review/items/${item.subject_id}/include`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        production_run_id: item.run_id,
        pipeline_generation: item.pipeline_generation,
        ...identity,
        ...(normalizedReason ? { reason: normalizedReason } : {}),
      }),
    },
  );
}

export async function excludeReviewItem(
  editionId: string,
  item: ReviewItem,
  reason: string,
): Promise<ReviewDecision> {
  return request(
    `/api/editions/${editionId}/review/items/${item.subject_id}/exclude`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        production_run_id: item.run_id,
        pipeline_generation: item.pipeline_generation,
        document_artifact_id: item.document_artifact_id,
        document_artifact_version: item.document_artifact_version,
        document_input_hash: item.document_input_hash,
        reason: reason.trim(),
      }),
    },
  );
}

export async function retryProductionRun(
  runId: string,
  stage: ReviewRetryStage,
): Promise<RetryProductionRunResponse> {
  return request(`/api/production/runs/${runId}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage }),
  });
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (response.ok) return (await response.json()) as T;
  throw await apiError(response);
}

async function apiError(response: Response): Promise<ApiError> {
  const body = (await response.json().catch(() => null)) as {
    detail?: { code?: unknown; message?: unknown } | string;
  } | null;
  const detail = body?.detail;
  const message =
    typeof detail === "string"
      ? detail
      : typeof detail?.message === "string"
        ? detail.message
        : "La revue de publication n’a pas pu être mise à jour.";
  const code =
    typeof detail === "object" &&
    detail !== null &&
    typeof detail.code === "string"
      ? detail.code
      : "publication_error";
  return new ApiError(message, code, response.status);
}
