import { ApiError, type EditionStatus } from "./editions";
import type { ProductionReconciliation } from "./production";

export type { ProductionReconciliation };

export type PublicationDecision = "include" | "exclude";

export type ReviewRunStatus =
  "queued" | "running" | "ready" | "needs_review" | "failed" | "cancelled";

export type ReviewRetryStage =
  "sources" | "references" | "extraction" | "synthesis" | "assembly";

export type ProductionRepairIssueKind =
  "rejected_indicator" | "rejected_rule" | "supplemental_source_unarchived";

export type ProductionRepairAction =
  "include" | "exclude" | "continue_without_source";

/**
 * Durable state of a Q1 proposal missing from the current canonical
 * ReferenceReport. `archived_pending_references` means the analyst already
 * supplied the content and only the deterministic rebuild is missing: the
 * backend keeps that debt, so a refresh can never erase it.
 */
export type SupplementalSourceRepairState =
  "unarchived" | "collection_missing" | "archived_pending_references";

export type AssemblyJobStatus =
  "queued" | "running" | "waiting_human" | "succeeded" | "failed" | "cancelled";

export interface PublicationAcceptResponse {
  edition_id: string;
  edition_status: EditionStatus;
  manifest_id: string;
  manifest_sha256: string;
  edition_version: number;
  batch_id: string;
  job_id: string | null;
  job_dispatched: boolean;
}

export interface EditionReleaseResponse {
  edition_id: string;
  edition_status: EditionStatus;
  manifest_id: string | null;
  manifest_sha256: string | null;
  release_id: string | null;
  json_available: boolean;
  markdown_available: boolean;
  docx_available: boolean;
  published_at: string | null;
  assembly_job_id: string | null;
  assembly_status: AssemblyJobStatus | null;
  assembly_error_code: string | null;
  assembly_error_message: string | null;
  can_retry_assembly: boolean;
}

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
  rejected_indicator_count: number;
  rejected_ioc_count?: number;
  rejected_other_artifact_count?: number;
  rejected_rule_count: number;
  published_rule_count: number;
  active_repair_count?: number;
  unresolved_repair_count?: number;
  /**
   * The backend owns the Review action policy. `can_retry` and
   * `requires_reconciliation` are mutually exclusive, so the UI never has to
   * infer which action is legal from an error message.
   */
  can_retry: boolean;
  retry_stage: ReviewRetryStage | null;
  requires_reconciliation: boolean;
  reconciliation: ProductionReconciliation | null;
  error_code: string | null;
  error_message: string | null;
}

export type EditionReviewItem = ReviewItem;

export interface EditionReview {
  edition_id: string;
  items: ReviewItem[];
  can_accept: boolean;
  unresolved_repair_count?: number;
  repair_review_complete?: boolean;
  pending_rebuild_count?: number;
}

export interface ProductionRepairDecision {
  id: string;
  action: ProductionRepairAction;
  actor_id: string;
  reason: string | null;
  created_at: string;
  observed_artifact_id: string;
  observed_pipeline_generation: number;
}

export interface EditionRepairItem {
  repair_key: string;
  kind: ProductionRepairIssueKind;
  position: number;
  subject_id: string;
  article_title: string;
  run_id: string;
  pipeline_generation: number;
  artifact_id: string | null;
  artifact_version: number | null;
  source_id: string | null;
  source_title: string | null;
  source_url: string | null;
  collection_id: string | null;
  collection_state: string | null;
  artifact_type: string | null;
  preview: string;
  reason_code: string;
  value_sha256: string;
  payload_available: boolean;
  effective_action: ProductionRepairAction | null;
  effective_decision_id: string | null;
  resolved: boolean;
  resolution_reason: string | null;
  rebuild_required: boolean;
  recommended_stage: string | null;
  repair_state?: SupplementalSourceRepairState | null;
  is_publication_ioc: boolean;
  /** False when the analyst excluded the article from the deliverable. */
  in_publication_scope?: boolean;
}

export interface EditionRepairSummary {
  unresolved_total: number;
  sources_to_supply: number;
  rejected_iocs_to_review: number;
  rejected_rules_to_review: number;
  rejected_other_artifacts: number;
  articles_with_repairs: number;
  articles_needing_rebuild: number;
}

export interface EditionRepairArticle {
  subject_id: string;
  has_pending_projection: boolean;
  recommended_stage: string;
  active_repair_count: number;
  resolved_since_last_build_count: number;
}

export interface EditionRepairPage {
  summary: EditionRepairSummary;
  items: EditionRepairItem[];
  articles: EditionRepairArticle[];
  next_cursor: string | null;
}

export interface EditionRepairDetail {
  repair_key: string;
  kind: ProductionRepairIssueKind;
  artifact_id?: string | null;
  artifact_version?: number | null;
  source_id: string | null;
  source_title: string | null;
  source_url: string | null;
  publisher?: string | null;
  artifact_type?: string | null;
  reason_code?: string | null;
  value_sha256?: string | null;
  preview?: string | null;
  payload_available?: boolean;
  value?: string | null;
  body?: string | null;
  collection_id?: string | null;
  collection_state?: string | null;
  repair_state?: SupplementalSourceRepairState | null;
  rebuild_required?: boolean;
  recommended_action?: string | null;
  effective_decision?: ProductionRepairDecision | null;
  /**
   * Complete append-only audit, oldest first. A decision is revisable, so the
   * last entry is the effective one and the earlier ones explain the change.
   */
  decision_history?: ProductionRepairDecision[];
}

export interface EditionRepairSourcePreparation {
  repair_key: string;
  subject_id: string;
  collection_id: string;
  collection_state: string;
  source_url: string;
}

export interface EditionRepairDecisionResponse {
  repair_key: string;
  decision_id: string;
  action: ProductionRepairAction;
  resolved: true;
}

export interface EditionRepairBulkDecisionResponse {
  decision_ids: string[];
  decisions: Array<{
    repair_key: string;
    decision_id: string;
    action: ProductionRepairAction;
  }>;
}

export interface EditionRepairRebuildResponse {
  action: string;
  stage: string | null;
  run_id: string;
  batch_id: string | null;
  changed: boolean;
  job_id: string | null;
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

export interface CancelProductionRunResponse {
  action: "cancel";
  run_id: string;
  status: "cancelled";
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

export async function getEditionRepairPage(
  editionId: string,
  options: {
    status?: "open" | "resolved" | "all";
    kind?: ProductionRepairIssueKind;
    subjectId?: string;
    artifactType?: string;
    cursor?: string | null;
    limit?: number;
  } = {},
): Promise<EditionRepairPage> {
  const params = new URLSearchParams({
    status: options.status ?? "all",
    limit: String(options.limit ?? 100),
  });
  if (options.kind) params.set("kind", options.kind);
  if (options.subjectId) params.set("subject_id", options.subjectId);
  if (options.artifactType) params.set("artifact_type", options.artifactType);
  if (options.cursor) params.set("cursor", options.cursor);
  const payload = await request<unknown>(
    `/api/editions/${encodeURIComponent(editionId)}/review/repairs?${params.toString()}`,
  );
  return isEditionRepairPage(payload) ? payload : emptyEditionRepairPage();
}

export function getEditionRepairDetail(
  editionId: string,
  repairKey: string,
): Promise<EditionRepairDetail> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/review/repairs/${encodeURIComponent(repairKey)}`,
  );
}

export function decideEditionRepair(
  editionId: string,
  repairKey: string,
  input: {
    action: ProductionRepairAction;
    observedSubjectId: string;
    observedRunId: string;
    observedArtifactId: string;
    observedPipelineGeneration: number;
    /**
     * Optimistic fence: null for a first decision, the id of the effective
     * decision currently displayed when the analyst revises it.
     */
    expectedEffectiveDecisionId: string | null;
    reason?: string | null;
  },
): Promise<EditionRepairDecisionResponse> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/review/repairs/${encodeURIComponent(repairKey)}/decision`,
    jsonRequest({
      action: input.action,
      observed_subject_id: input.observedSubjectId,
      observed_run_id: input.observedRunId,
      observed_artifact_id: input.observedArtifactId,
      observed_pipeline_generation: input.observedPipelineGeneration,
      expected_effective_decision_id: input.expectedEffectiveDecisionId,
      ...(input.reason?.trim() ? { reason: input.reason.trim() } : {}),
    }),
  );
}

/** Create or return the SourceCollection matching one raw Q1 proposal. */
export function prepareEditionRepairSource(
  editionId: string,
  repairKey: string,
): Promise<EditionRepairSourcePreparation> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/review/repairs/${encodeURIComponent(repairKey)}/source`,
    { method: "POST" },
  );
}

export function decideEditionRepairsBulk(
  editionId: string,
  decisions: ReadonlyArray<{
    repairKey: string;
    action: ProductionRepairAction;
    observedSubjectId: string;
    observedRunId: string;
    observedArtifactId: string;
    observedPipelineGeneration: number;
    expectedEffectiveDecisionId: string | null;
  }>,
  reason?: string | null,
): Promise<EditionRepairBulkDecisionResponse> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/review/repairs/decisions`,
    jsonRequest({
      decisions: decisions.map((decision) => ({
        repair_key: decision.repairKey,
        action: decision.action,
        observed_subject_id: decision.observedSubjectId,
        observed_run_id: decision.observedRunId,
        observed_artifact_id: decision.observedArtifactId,
        observed_pipeline_generation: decision.observedPipelineGeneration,
        expected_effective_decision_id: decision.expectedEffectiveDecisionId,
      })),
      ...(reason?.trim() ? { reason: reason.trim() } : {}),
    }),
  );
}

export function rebuildEditionReviewItem(
  editionId: string,
  subjectId: string,
  observed?: {
    runId?: string;
    pipelineGeneration?: number;
    artifactId?: string;
  },
): Promise<EditionRepairRebuildResponse> {
  const body = observed
    ? {
        ...(observed.runId ? { observed_run_id: observed.runId } : {}),
        ...(observed.pipelineGeneration !== undefined
          ? { observed_pipeline_generation: observed.pipelineGeneration }
          : {}),
        ...(observed.artifactId
          ? { observed_artifact_id: observed.artifactId }
          : {}),
      }
    : undefined;
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/review/items/${encodeURIComponent(subjectId)}/rebuild`,
    body ? jsonRequest(body) : { method: "POST" },
  );
}

export function acceptEditionPublication(
  editionId: string,
): Promise<PublicationAcceptResponse> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/publication/accept`,
    {
      method: "POST",
    },
  );
}

export function getEditionRelease(
  editionId: string,
): Promise<EditionReleaseResponse> {
  return request(`/api/editions/${encodeURIComponent(editionId)}/release`);
}

export function editionDocxUrl(editionId: string): string {
  return `/api/editions/${encodeURIComponent(editionId)}/release/docx`;
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

export async function cancelProductionRun(
  runId: string,
): Promise<CancelProductionRunResponse> {
  return request(`/api/production/runs/${runId}/cancel`, {
    method: "POST",
  });
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (response.ok) return (await response.json()) as T;
  throw await apiError(response);
}

function jsonRequest(body: object): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function isEditionRepairPage(value: unknown): value is EditionRepairPage {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    Array.isArray(record.items) &&
    Array.isArray(record.articles) &&
    typeof record.summary === "object" &&
    record.summary !== null
  );
}

function emptyEditionRepairPage(): EditionRepairPage {
  return {
    summary: {
      unresolved_total: 0,
      sources_to_supply: 0,
      rejected_iocs_to_review: 0,
      rejected_rules_to_review: 0,
      rejected_other_artifacts: 0,
      articles_with_repairs: 0,
      articles_needing_rebuild: 0,
    },
    items: [],
    articles: [],
    next_cursor: null,
  };
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
