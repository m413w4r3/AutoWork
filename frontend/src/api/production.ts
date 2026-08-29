import { ApiError } from "./editions";

export type SubjectProductionStatus =
  "queued" | "running" | "ready" | "needs_review" | "failed" | "cancelled";

export type SubjectProductionStage =
  "sources" | "references" | "extraction" | "synthesis" | "assembly";

export type ProductionBatchPhase = "initial" | "recovery" | "review";

type ProductionBatchStatus =
  "queued" | "running" | "completed" | "completed_with_issues" | "cancelled";

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
  status: SubjectProductionStatus;
  current_stage: SubjectProductionStage;
  progress_current: number;
  progress_total: number;
  references_conversation_id: string | null;
  synthesis_conversation_id: string | null;
  run_id: string;
  pipeline_generation: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_message: string | null;
  error_details: Record<string, unknown> | null;
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
  status: SubjectProductionStatus;
  current_stage: SubjectProductionStage;
  pipeline_generation: number;
  auto_recovery_count: number;
  error_code: string | null;
  error_message: string | null;
}

export interface BatchStatus {
  batch_id: string;
  edition_id: string;
  status: ProductionBatchStatus;
  phase: ProductionBatchPhase;
  next_dispatch_at: string | null;
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
  /** Publication Markdown is downloadable alongside the canonical document. */
  rendered_content: string | null;
  canonical_content:
    PublicationDocument | ExtractionDocumentV2 | Record<string, unknown> | null;
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
  source_ids: string[];
}

export interface ExtractionDocumentV2 {
  schema_version: "2";
  parser_version: string;
  items: ExtractionItemV2[];
  uncertainties: string[];
}

export interface ProductionStateSnapshotV1 {
  format: "autowork.production-state";
  schema_version: 1;
  exported_at: string;
  origin: {
    subject_title: string;
    editorial_type: "brief";
    profile: "brief_auto";
    research_date: string | null;
  };
  artifacts: {
    references: {
      input_hash: string;
      canonical_content: Record<string, unknown>;
    };
    extraction: {
      input_hash: string;
      canonical_content: ExtractionDocumentV2;
    };
    synthesis: {
      input_hash: string;
      rendered_content: string;
    };
  };
  content_sha256: string;
}

export interface ProductionStateSnapshotV2 {
  format: "autowork.production-state";
  schema_version: 2;
  exported_at: string;
  origin: {
    subject_title: string;
    research_date: string | null;
  };
  artifacts: ProductionStateSnapshotV1["artifacts"];
  content_sha256: string;
}

export type ProductionStateSnapshot =
  ProductionStateSnapshotV1 | ProductionStateSnapshotV2;

export interface ProductionStateImportResult {
  run_id: string;
  status: "needs_review";
  current_stage: "assembly";
  imported_stages: ["references", "extraction", "synthesis"];
  schema_version: 1 | 2;
  content_sha256: string;
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
  analyst_note?: RichSpan[] | null;
  original_indicators?: Array<{
    artifact_type: string;
    values: Array<{
      value: string;
      normalized_value: string;
      artifact_type: string;
      source_ids: string[];
    }>;
  }>;
}

export interface PublicationDocumentV2 extends Omit<
  BriefDocumentV1,
  "schema_version"
> {
  schema_version: "2";
}

export type PublicationDocument = BriefDocumentV1 | PublicationDocumentV2;

/**
 * Start production of a subject.
 *
 * The edition is resolved server-side from the subject's editorial group, so
 * the subject page only needs the subject id.
 */
export async function startSubjectProduction(
  subjectId: string,
): Promise<{ run_id: string; status: string }> {
  return request(`/api/subjects/${subjectId}/production`, {
    method: "POST",
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

export async function exportProductionState(
  subjectId: string,
): Promise<ProductionStateSnapshotV2> {
  return request(`/api/subjects/${subjectId}/production/state/export`);
}

export async function importProductionState(
  subjectId: string,
  snapshot: ProductionStateSnapshot,
): Promise<ProductionStateImportResult> {
  return request(`/api/subjects/${subjectId}/production/state/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(snapshot),
  });
}

/** Recompute one stage in place, invalidating its downstream artifacts. */
export async function retryProductionStage(
  subjectId: string,
  stage: "sources" | "references" | "extraction" | "synthesis" | "assembly",
): Promise<{
  run_id: string;
  status: string;
  job_id: string | null;
  pipeline_generation: number;
}> {
  return request(`/api/subjects/${subjectId}/production/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage }),
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

export async function getPublicationArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(`/api/subjects/${subjectId}/production/artifacts/publication`);
}

// Edition production API

/**
 * Start batch production for an edition.
 *
 * Without a subject list, every eligible subject of the edition is produced.
 */
export async function startEditionProduction(
  editionId: string,
): Promise<BatchStatus> {
  return request(`/api/editions/${editionId}/production`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subject_ids: null }),
  });
}

/**
 * Get batch production status.
 *
 * Returns null when no batch has been started yet.
 */
export async function getEditionProduction(
  editionId: string,
): Promise<BatchStatus | null> {
  return requestOrNull(`/api/editions/${editionId}/production`);
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
