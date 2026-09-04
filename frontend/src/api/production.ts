import { ApiError } from "./editions";

export type SubjectProductionStatus =
  "queued" | "running" | "ready" | "needs_review" | "failed" | "cancelled";

export type SubjectProductionStage =
  "sources" | "references" | "extraction" | "synthesis" | "assembly";

export type ProductionBatchPhase = "initial" | "recovery" | "review";
export type ProductionRecoveryDisposition = "auto" | "manual_only";

export type ExtractionProgressProfile = "full" | "ioc_rules";
export type ExtractionProgressSourceStatus =
  | "pending"
  | "running"
  | "cached"
  | "succeeded"
  | "needs_review"
  | "failed"
  | "skipped";

export interface ExtractionProgressSourceSkip {
  source_url?: string | null;
  reason_code?: string | null;
  live_error_code?: string | null;
  archive_error_code?: string | null;
  archive_reason?: string | null;
  blocking?: boolean;
  [key: string]: unknown;
}

export interface ExtractionProgressSource {
  source_id: string;
  title: string;
  profile: ExtractionProgressProfile;
  status: ExtractionProgressSourceStatus;
  ioc_count: number;
  rule_count: number;
  source_url?: string | null;
  url?: string | null;
  skip?: ExtractionProgressSourceSkip | null;
  access_mode?: "live_url" | "archive_fallback" | null;
  archive_fallback?: boolean;
}

export interface ExtractionProgress {
  total_sources: number;
  completed_sources: number;
  full_total: number;
  full_completed: number;
  ioc_rules_total: number;
  ioc_rules_completed: number;
  cache_hits: number;
  model_calls: number;
  confirmed_iocs: number;
  contextual_iocs: number;
  rules_total: number;
  yara_rules: number;
  sigma_rules: number;
  suricata_rules: number;
  snort_rules: number;
  active_source_id: string | null;
  active_source_title: string | null;
  active_profile: ExtractionProgressProfile | null;
  sources: ExtractionProgressSource[];
  skipped_sources?: number;
  skipped_source_ids?: string[];
  source_skips?: Record<string, ExtractionProgressSourceSkip>;
}

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
  reused?: boolean;
  reused_from_artifact_id?: string | null;
  reused_from_created_at?: string | null;
  research_date?: string | null;
  /** Short user-facing progress detail, when the pipeline exposes one. */
  detail?: string;
  /** Only on the sources stage. */
  archived_sources?: number;
}

export interface ProductionStatus {
  subject_id: string;
  edition_id: string;
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
  recovery_disposition: ProductionRecoveryDisposition;
  extraction_progress?: ExtractionProgress | null;
  reconciliation?: ProductionReconciliation | null;
  /**
   * Set when the run belongs to an edition production batch. Such a run is
   * only ever resumed through its batch, never restarted as a standalone one.
   */
  batch_id?: string | null;
  /** Parser recoveries worth showing, never blocking. */
  warnings: string[];
  stages: Record<string, StageStatus>;
}

export interface ProductionReconciliation {
  production_run_id: string;
  model_run_id: string;
  bridge_response_id: string | null;
  submission_state: string;
  phase: string;
  stage: SubjectProductionStage;
  pipeline_generation: number;
  output_sha256: string | null;
  provenance: string | null;
  visible_available: boolean;
  batch_id: string | null;
}

export interface ProductionRecoveryPreview {
  production_run_id: string;
  model_run_id: string;
  stage: string;
  pipeline_generation: number;
  bridge_response_id: string | null;
  submission_state: string;
  phase: string;
  text: string;
  sha256: string;
  chars: number;
  metadata: Record<string, unknown>;
  visible_available: boolean;
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
  extraction_progress?: ExtractionProgress | null;
  reconciliation?: ProductionReconciliation | null;
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

export interface CancelProductionBatchResponse {
  action: string;
  batch_id: string;
  status: "cancelled";
  edition_status: "selection";
  edition_version: number;
}

export interface ArtifactResponse {
  artifact_id: string;
  stage: string;
  version: number;
  status: "verified" | "stale" | "needs_review";
  reused?: boolean;
  reused_from_artifact_id?: string | null;
  reused_from_created_at?: string | null;
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

export async function restartProductionWithNewSources(
  subjectId: string,
): Promise<{ run_id: string; replaced_run_id: string }> {
  return request(
    `/api/production/subjects/${encodeURIComponent(subjectId)}/production/restart-with-new-sources`,
    { method: "POST" },
  );
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

export async function previewProductionReconciliationVisible(
  runId: string,
): Promise<ProductionRecoveryPreview> {
  return request(
    `/api/production/runs/${runId}/reconciliation/visible/preview`,
    {
      method: "POST",
    },
  );
}

export async function adoptProductionReconciliationVisible(
  runId: string,
  expectedSha256: string,
): Promise<Record<string, unknown>> {
  return request(`/api/production/runs/${runId}/reconciliation/visible/adopt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_sha256: expectedSha256 }),
  });
}

export async function previewProductionReconciliationManual(
  runId: string,
  markdown: string,
): Promise<ProductionRecoveryPreview> {
  return request(
    `/api/production/runs/${runId}/reconciliation/manual/preview`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown }),
    },
  );
}

export async function adoptProductionReconciliationManual(
  runId: string,
  markdown: string,
  expectedSha256: string,
): Promise<Record<string, unknown>> {
  return request(`/api/production/runs/${runId}/reconciliation/manual/adopt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ markdown, expected_sha256: expectedSha256 }),
  });
}

/** Block future cross-run reuse from this costly stage onward. */
export async function invalidateProductionReuse(
  subjectId: string,
  fromStage: "references" | "extraction" | "synthesis",
): Promise<{ action: string; from_stage: string; occurred_at: string }> {
  return request(`/api/subjects/${subjectId}/production/reuse/invalidate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ from_stage: fromStage }),
  });
}

export async function cancelProductionBatch(
  editionId: string,
  batchId: string,
): Promise<CancelProductionBatchResponse> {
  return request(`/api/editions/${editionId}/production/${batchId}/cancel`, {
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
 * Start batch production for an edition, for exactly the given subjects.
 *
 * `subjectIds` must be the operator's explicit production-batch selection —
 * a subset of the editorially eligible subjects, in editorial board order.
 * Editorial eligibility (`EditorialGroup.status === "selected"`) is a
 * separate notion from this batch selection: subjects left unchecked are
 * never sent here and keep whatever editorial decision they already have.
 * An empty selection is refused client-side rather than silently falling
 * back to "every eligible subject" — the caller must ask the operator to
 * choose at least one subject.
 */
export async function startEditionProduction(
  editionId: string,
  subjectIds: readonly string[],
): Promise<BatchStatus> {
  if (subjectIds.length === 0) {
    throw new Error(
      "startEditionProduction requires at least one selected subject",
    );
  }
  return request(`/api/editions/${editionId}/production`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subject_ids: subjectIds }),
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
