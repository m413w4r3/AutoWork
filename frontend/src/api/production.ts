import { ApiError } from "./editions";

export interface StageStatus {
  status:
    | "pending"
    | "running"
    | "succeeded"
    | "needs_review"
    | "failed"
    | "cancelled";
  version: number | null;
  error_code: string | null;
  error_message: string | null;
  /** Short user-facing progress detail, when the pipeline exposes one. */
  detail?: string;
  /** Only on the sources stage. */
  archived_sources?: number;
}

export interface ProductionStatus {
  subject_id: string;
  title: string;
  editorial_type: string;
  status:
    "queued" | "running" | "ready" | "needs_review" | "failed" | "cancelled";
  current_stage: string;
  progress_current: number;
  progress_total: number;
  references_conversation_id: string | null;
  synthesis_conversation_id: string | null;
  run_id: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** Parser recoveries worth showing, never blocking. */
  warnings: string[];
  stages: Record<string, StageStatus>;
}

export function shouldPollProduction(
  status: ProductionStatus["status"] | undefined,
): boolean {
  return status === "queued" || status === "running";
}

export interface BatchItemDetail {
  position: number;
  subject_id: string;
  title: string;
  run_id: string;
  status: string;
  current_stage: string;
}

export interface BatchStatus {
  batch_id: string;
  edition_id: string;
  profile: string;
  status:
    "queued" | "running" | "completed" | "completed_with_issues" | "cancelled";
  items: number;
  completed: number;
  needs_review: number;
  failed: number;
  cancelled: number;
  item_details: BatchItemDetail[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ArtifactResponse {
  artifact_id: string;
  stage: string;
  version: number;
  status: "verified" | "stale" | "needs_review";
  metadata: Record<string, unknown>;
  /** Publication Markdown is downloadable, not the BRIEF preview source. */
  rendered_content: string | null;
  canonical_content:
    BriefDocumentV1 | ExtractionDocumentV2 | Record<string, unknown> | null;
}

export interface ExtractionItemV2 {
  id: string;
  category: string;
  value: string;
  context: string;
  artifact_type: string | null;
  semantic_type: string;
  indicator_status:
    "confirmed_ioc" | "contextual" | "excluded" | "not_applicable";
  provenance: string;
  display_policy: "ioc_section" | "body_only" | "both" | "hidden";
  normalized_value: string | null;
  evidence_quote: string | null;
  source_document_ids: string[];
  chunk_ids: string[];
  model_run_ids: string[];
  attack_id: string | null;
  reference_ids: string[];
  source_ids: string[];
  supported: boolean;
}

export interface ExtractionDocumentV2 {
  schema_version: "2";
  parser_version: string;
  items: ExtractionItemV2[];
  uncertainties: string[];
}

export type RichSpanKind =
  | "text"
  | "emphasis"
  | "actor"
  | "malware"
  | "tool"
  | "product"
  | "technical"
  | "ioc"
  | "code"
  | "citation";

export interface RichSpan {
  kind: RichSpanKind;
  text: string;
  source_ids: string[];
}

export interface BriefDocumentV1 {
  schema_version: "1";
  title: string;
  timeline: Array<{
    date: string | null;
    content: RichSpan[];
    source_ids: string[];
  }>;
  synthesis: RichSpan[][];
  indicators: Array<{
    artifact_type: string;
    values: Array<{
      value: string;
      normalized_value: string;
      artifact_type: string;
      source_ids: string[];
    }>;
  }>;
  sources: Array<{ source_id: string; canonical_url: string }>;
  uncertainties: string[];
}

/**
 * Start production of a subject.
 *
 * The edition is resolved server-side from the subject's editorial group, so
 * the subject page only needs the subject id.
 */
export async function startSubjectProduction(
  subjectId: string,
  profile: "brief_auto" | "major_assisted" = "brief_auto",
): Promise<{ run_id: string; status: string }> {
  return request(`/api/subjects/${subjectId}/production`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ profile }),
  });
}

/**
 * Get production status for a subject.
 *
 * Returns null when no production has been started yet — that is a normal
 * state, not an error, and it is what makes the UI offer a start button.
 */
export async function getSubjectProduction(
  subjectId: string,
): Promise<ProductionStatus | null> {
  return requestOrNull(`/api/subjects/${subjectId}/production`);
}

export async function retryReferences(
  subjectId: string,
): Promise<{ status: string }> {
  return request(`/api/subjects/${subjectId}/production/references/retry`, {
    method: "POST",
  });
}

export async function retrySynthesis(
  subjectId: string,
): Promise<{ status: string }> {
  return request(`/api/subjects/${subjectId}/production/synthesis/retry`, {
    method: "POST",
  });
}

export async function cancelSubjectProduction(
  subjectId: string,
): Promise<{ status: string }> {
  return request(`/api/subjects/${subjectId}/production/cancel`, {
    method: "POST",
  });
}

export async function getReferencesArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(`/api/subjects/${subjectId}/production/artifacts/references`);
}

export async function getExtractionArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(`/api/subjects/${subjectId}/production/artifacts/extraction`);
}

export async function getSynthesisArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(`/api/subjects/${subjectId}/production/artifacts/synthesis`);
}

export async function getBriefArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(`/api/subjects/${subjectId}/production/artifacts/brief`);
}

export async function saveBriefDraft(
  subjectId: string,
  content: string,
): Promise<{ artifact_id: string; saved_at: string; draft_version: number }> {
  return request(`/api/subjects/${subjectId}/production/brief/draft`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
}

export async function getBriefDraft(subjectId: string): Promise<{
  content: string;
  saved_at: string;
  draft_version: number;
} | null> {
  return requestOrNull(`/api/subjects/${subjectId}/production/brief/draft`);
}

// Edition Production API

/**
 * Start batch production for an edition.
 *
 * Without `subjectIds` every selected brief of the edition is produced;
 * with it, only that subset.
 */
export async function startEditionBriefProduction(
  editionId: string,
  subjectIds?: string[],
): Promise<BatchStatus> {
  return request(`/api/editions/${editionId}/production/briefs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subject_ids: subjectIds ?? null }),
  });
}

/**
 * Get batch production status.
 *
 * Returns null when no batch has been started yet.
 */
export async function getEditionBriefProduction(
  editionId: string,
): Promise<BatchStatus | null> {
  return requestOrNull(`/api/editions/${editionId}/production/briefs`);
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (response.ok) return (await response.json()) as T;
  throw await apiError(response);
}

/**
 * Like `request`, but treats 404 as "nothing here yet" rather than a failure.
 */
async function requestOrNull<T>(
  url: string,
  init?: RequestInit,
): Promise<T | null> {
  const response = await fetch(url, init);
  if (response.status === 404) return null;
  if (response.ok) return (await response.json()) as T;
  throw await apiError(response);
}

async function apiError(response: Response): Promise<ApiError> {
  const body = (await response.json().catch(() => null)) as {
    detail?: { code?: string; message?: string } | string;
  } | null;
  const detail = body?.detail;
  const message =
    typeof detail === "string"
      ? detail
      : detail?.message ||
        "La production n\u2019a pas pu \u00eatre effectu\u00e9e.";
  return new ApiError(
    message,
    typeof detail === "object" && detail?.code
      ? detail.code
      : "production_error",
    response.status,
  );
}
