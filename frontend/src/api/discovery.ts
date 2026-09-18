import { ApiError } from "./editions";
import type { JobStatus, JobView } from "./jobs";

export type SourceRole =
  "primary" | "independent" | "relay" | "aggregator" | "social" | "unknown";

export type SourceVerificationStatus =
  "unverified" | "verify_later" | "invalid" | "unavailable";

export interface SourceCandidate {
  id: string;
  url: string;
  canonical_url: string;
  raw_url: string | null;
  local_ref: string | null;
  source_ref: string;
  title: string;
  publisher: string;
  role: SourceRole;
  published_at: string | null;
  event_date: string | null;
  citation: string | null;
  period_relation: "in_period" | "outside_period" | "unknown";
  ioc_presence: "none" | "declared" | "visible" | "unknown";
  ioc_declared_count: number | null;
  ioc_visible_count: number | null;
  parsing_warnings: string[];
  verification_status: SourceVerificationStatus;
  relationship_status: "provisional" | "verified";
  verification_changed_at: string | null;
  verification_changed_by: string | null;
}

export interface IncompleteSourceCandidate {
  id: string;
  title: string;
  publisher: string;
  raw_url: string | null;
  local_ref: string | null;
  published_at: string | null;
  period_relation: "in_period" | "outside_period" | "unknown";
  role: SourceRole;
  ioc_presence: "none" | "declared" | "visible" | "unknown";
  ioc_declared_count: number | null;
  ioc_visible_count: number | null;
  parsing_warnings: string[];
}

export interface DiscoveryCandidate {
  id: string;
  discovery_run_id: string;
  discovery_batch_id: string;
  created_at: string;
  title: string;
  summary: string;
  novelty: string;
  technical_potential: number;
  event_date: string | null;
  uncertainties: string[];
  relevance_reasons: string[];
  actors: string[];
  campaigns: string[];
  malware: string[];
  cves: string[];
  victims: string[];
  sectors: string[];
  countries: string[];
  likely_artifacts: string[];
  iocs: string[];
  provisional_iocs?: ProvisionalDiscoveryIoc[];
  provisional_ioc_count?: number;
  provisional_ioc_type_counts?: Record<string, number>;
  has_publisher_ioc_count?: boolean;
  sources: SourceCandidate[];
  incomplete_sources: IncompleteSourceCandidate[];
  local_ref: string | null;
  actor_or_campaign: string;
  technical_potential_reason: string;
  parsing_warnings: string[];
  context_only: boolean;
  selectable: boolean;
  valid_publication_count: number;
  incomplete_publication_count: number;
}

export interface ProvisionalDiscoveryIoc {
  id: string;
  raw_value: string;
  normalized_value: string | null;
  declared_type: string;
  proposed_type:
    | "ipv4"
    | "ipv6"
    | "domain"
    | "url"
    | "md5"
    | "sha1"
    | "sha256"
    | "email"
    | "cve"
    | "other"
    | "unknown";
  status: "provisional_visible";
  publication_refs: string[];
  // Pairs with publication_refs, but carries the surviving SourceCandidate.id
  // for each relation instead of the (batch-local, so collision-prone once
  // several research batches are consolidated) local_ref string.
  publication_ids: string[];
  warnings: string[];
}

export interface DiscoveryBatch {
  id: string;
  discovery_run_id: string;
  complementary_axis: string;
  queries: string[];
  citations: Array<{ label: string; url: string; excerpt: string | null }>;
  discovery_model_run_id: string;
  created_at: string;
  source_mode:
    | "native_complete"
    | "visible_citations_only"
    | "model_declared_urls"
    | "manual_import";
  bridge_capabilities: Record<string, unknown>;
  citation_count: number;
  source_coverage_complete: boolean;
  source_coverage_incomplete_reason: string | null;
  report_sha256: string | null;
  parser_version: string;
  parsing_status: string;
  parsing_warnings: string[];
  unattached_visible_citations: Array<{
    label: string;
    url: string;
    canonical_url: string;
    excerpt: string | null;
  }>;
  parsing_revision: number;
  supersedes_batch_id: string | null;
  replaced_by_batch_id: string | null;
  is_active_revision: boolean;
  archived_report_url: string;
}

export interface DiscoveryResult {
  batches: DiscoveryBatch[];
  candidates: DiscoveryCandidate[];
  total: number;
  warning: string;
}

/** Résultat d'une action Job (recovery, reprocessing) : jamais une identité de run. */
export interface DiscoveryJobActionResult {
  job_id: string;
  status: JobStatus;
  reused: boolean;
}

export type DiscoveryInputMode = "bridge_research" | "manual_import";

export interface DiscoveryRunRequestSnapshot {
  country: string;
  country_code: string;
  country_aliases: string[];
  period_start: string;
  period_end: string;
  as_of_date: string;
  languages: string[];
  source_profile: string;
  keywords: string[];
  exclusions: string[];
  complementary_axis: string;
  tlp: string;
  sensitivity: string;
  external_llm_allowed: boolean;
}

export interface DiscoveryRunExecution {
  job_id: string;
  status: JobStatus;
  progress_current: number;
  progress_total: number;
  user_message: string | null;
  error_code: string | null;
  error_message: string | null;
  error_details: JobView["error_details"];
  started_at: string | null;
  finished_at: string | null;
}

export interface DiscoveryRunResult {
  batch_id: string;
  research_model_run_id: string;
  archived_report_url: string;
}

export interface DiscoveryRun {
  run_id: string;
  edition_id: string;
  input_mode: DiscoveryInputMode;
  source_profile: string;
  complementary_axis: string;
  request_snapshot: Readonly<DiscoveryRunRequestSnapshot>;
  created_by: string;
  created_at: string;
  execution: DiscoveryRunExecution | null;
  result: DiscoveryRunResult | null;
}

export interface DiscoveryRecoveryPreview {
  sha256: string;
  subject_count: number;
  publication_count: number;
  ioc_count: number;
  ioc_type_counts: Record<string, number>;
  warnings: string[];
  subjects: string[];
}

export interface DiscoveryImportConfirmResult {
  run_id: string;
  batch_id: string;
  reused: boolean;
  source_mode: "manual_import";
  subject_count: number;
  publication_count: number;
  // Consolidation into subjects runs in an async job dispatched right after
  // this call returns; null when reused (already consolidated earlier).
  // Callers must poll this job and only refresh discovery state once it is
  // terminal — refreshing immediately races the job.
  reconciliation_job_id: string | null;
}

export interface DiscoveryLaunchPayload {
  complementary_axis: string;
  source_profile: string;
}

export function launchDiscoveryRun(
  editionId: string,
  payload: DiscoveryLaunchPayload,
  idempotencyKey: string,
): Promise<DiscoveryRun> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/runs`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(payload),
    },
  );
}

export function fetchDiscoveryRuns(editionId: string): Promise<DiscoveryRun[]> {
  return request<DiscoveryRun[]>(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/runs`,
  );
}

export function reprocessReport(
  editionId: string,
  runId: string,
  researchModelRunId: string,
  idempotencyKey: string,
): Promise<DiscoveryJobActionResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/reports/reprocess`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({
        run_id: runId,
        research_model_run_id: researchModelRunId,
      }),
    },
  );
}

export function previewVisibleDiscoveryRecovery(
  editionId: string,
  modelRunId: string,
  jobId: string,
): Promise<DiscoveryRecoveryPreview> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/recovery/${encodeURIComponent(modelRunId)}/visible/preview`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId }),
    },
  );
}

export function confirmVisibleDiscoveryRecovery(
  editionId: string,
  modelRunId: string,
  jobId: string,
  expectedSha256: string,
): Promise<DiscoveryJobActionResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/recovery/${encodeURIComponent(modelRunId)}/visible/confirm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: jobId,
        expected_sha256: expectedSha256,
      }),
    },
  );
}

export function requestDiscoveryCompletion(
  editionId: string,
  modelRunId: string,
  jobId: string,
): Promise<DiscoveryJobActionResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/recovery/${encodeURIComponent(modelRunId)}/complete`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId }),
    },
  );
}

export function previewManualDiscoveryRecovery(
  editionId: string,
  modelRunId: string,
  jobId: string,
  markdown: string,
): Promise<DiscoveryRecoveryPreview> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/recovery/${encodeURIComponent(modelRunId)}/manual/preview`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, markdown }),
    },
  );
}

export function confirmManualDiscoveryRecovery(
  editionId: string,
  modelRunId: string,
  jobId: string,
  markdown: string,
  expectedSha256: string,
): Promise<DiscoveryJobActionResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/recovery/${encodeURIComponent(modelRunId)}/manual/confirm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: jobId,
        markdown,
        expected_sha256: expectedSha256,
      }),
    },
  );
}

export function previewDiscoveryImport(
  editionId: string,
  markdown: string,
  sourceProfile: string,
  complementaryAxis: string = "manual-import",
  sensitivity: string = "internal",
): Promise<DiscoveryRecoveryPreview> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/import/preview`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        markdown,
        source_profile: sourceProfile,
        complementary_axis: complementaryAxis,
        sensitivity,
        external_llm_allowed: true,
      }),
    },
  );
}

export function confirmDiscoveryImport(
  editionId: string,
  markdown: string,
  expectedSha256: string,
  sourceProfile: string,
  idempotencyKey: string,
  complementaryAxis: string = "manual-import",
  sensitivity: string = "internal",
): Promise<DiscoveryImportConfirmResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/import/confirm`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({
        markdown,
        expected_sha256: expectedSha256,
        source_profile: sourceProfile,
        complementary_axis: complementaryAxis,
        sensitivity,
        external_llm_allowed: true,
      }),
    },
  );
}

export function fetchDiscovery(
  editionId: string,
  filters: {
    search: string;
    minTechnicalPotential: number;
    sourceStatus: SourceVerificationStatus | "";
    sort: "newest" | "technical" | "novelty" | "title";
  },
): Promise<DiscoveryResult> {
  const parameters = new URLSearchParams({
    min_technical_potential: String(filters.minTechnicalPotential),
    sort: filters.sort,
  });
  if (filters.search) parameters.set("search", filters.search);
  if (filters.sourceStatus)
    parameters.set("source_status", filters.sourceStatus);
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/candidates?${parameters.toString()}`,
  );
}

export function fetchDiscoveryRunCandidates(
  editionId: string,
  runId: string,
): Promise<DiscoveryCandidate[]> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/runs/${encodeURIComponent(runId)}/candidates`,
  );
}

export type MergeDecisionAction =
  "accept" | "create_new" | "attach_to" | "merge_existing" | "defer";

export interface MergeHandleLabel {
  handle: string;
  title: string;
  summary: string;
  source_urls: string[];
}

export interface MergeGroupDiff {
  group_index: number;
  existing_subject_handles: string[];
  incoming_candidate_handles: string[];
  disposition: "apply" | "review" | null;
  flags: string[];
  confidence: "high" | "medium" | "low" | null;
  rationale: string;
  evidence: {
    shared_publication_urls?: string[];
    shared_campaigns?: string[];
    shared_malware?: string[];
    shared_explicit_identifiers?: string[];
    semantic_basis?: string[];
    conflict_signals?: string[];
  };
}

export interface MergeRun {
  id: string;
  edition_id: string;
  parent_snapshot_id: string | null;
  intake_id: string;
  planner_kind: string;
  validation_status: "valid" | "repaired" | "invalid" | "needs_review";
  review_reasons: string[];
  warnings: string[];
  projected_diff: MergeGroupDiff[];
  handle_labels: Record<string, MergeHandleLabel>;
  supersedes_merge_run_id: string | null;
  created_at: string;
}

export interface MergeResolution {
  snapshot_id: string;
  snapshot_version: number;
}

export function listMergeRuns(editionId: string): Promise<MergeRun[]> {
  return request(`/api/editions/${encodeURIComponent(editionId)}/merge-runs`);
}

export function readMergeRun(
  editionId: string,
  runId: string,
): Promise<MergeRun> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/merge-runs/${encodeURIComponent(runId)}`,
  );
}

export function resolveMergeRun(
  editionId: string,
  runId: string,
  decisions: Array<{
    group_index: number;
    action: MergeDecisionAction;
    target_subject_handle?: string | null;
  }>,
): Promise<MergeResolution> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/merge-runs/${encodeURIComponent(runId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group_decisions: decisions }),
    },
  );
}

export function markDiscoverySource(
  editionId: string,
  candidateId: string,
  sourceId: string,
  status: SourceVerificationStatus,
): Promise<SourceCandidate> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/candidates/${encodeURIComponent(candidateId)}/sources/${encodeURIComponent(sourceId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    },
  );
}

export interface IncompleteSourceAttachmentResult {
  source: SourceCandidate;
  updated_subject_ids: string[];
}

export function attachIncompleteSourceUrl(
  editionId: string,
  candidateId: string,
  incompleteSourceId: string,
  url: string,
): Promise<IncompleteSourceAttachmentResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/candidates/` +
      `${encodeURIComponent(candidateId)}/incomplete-sources/${encodeURIComponent(incompleteSourceId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    },
  );
}

// Used by the selected-Subject pipeline, which only knows the Subject id: the
// backend resolves the persisted DiscoveryCandidate carrying the replaced URL.
// Temporary adapter until AW-008 replaces the legacy Selection projection.
export function attachReplacementSourceUrl(
  editionId: string,
  subjectId: string,
  replacedCanonicalUrl: string,
  url: string,
): Promise<IncompleteSourceAttachmentResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/subjects/` +
      `${encodeURIComponent(subjectId)}/sources/replacement`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        replaced_canonical_url: replacedCanonicalUrl,
        url,
      }),
    },
  );
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (response.ok) return (await response.json()) as T;
  const body = (await response.json().catch(() => null)) as {
    detail?: { code?: string; message?: string } | string;
  } | null;
  const detail = body?.detail;
  if (typeof detail === "object" && detail?.message) {
    throw new ApiError(
      detail.message,
      detail.code ?? "discovery_error",
      response.status,
    );
  }
  // A 500 carries FastAPI's bare "Internal Server Error" string, so the only
  // thing worth telling the user is that the fault is server-side and where to
  // look for it — an unqualified "l’opération a échoué" sends them nowhere.
  const message =
    response.status >= 500
      ? `Erreur interne du serveur (HTTP ${response.status}). Le détail est dans le journal de diagnostic.`
      : `La découverte n’a pas pu être effectuée (HTTP ${response.status}).`;
  throw new ApiError(
    message,
    typeof detail === "object" && detail?.code
      ? detail.code
      : "discovery_error",
    response.status,
  );
}
