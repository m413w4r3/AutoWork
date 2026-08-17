import { ApiError } from "./editions";

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

export interface CandidateReference {
  batch_id: string;
  candidate_id: string;
}

export interface CandidateTopic {
  id: string;
  batch_id: string;
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
  editorial_status: "proposed";
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
  // Consolidation tracking (P2)
  member_references?: CandidateReference[];
  contribution_count?: number;
  duplicate_publication_count?: number;
  merge_warnings?: string[];
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
  warnings: string[];
}

export interface DiscoveryBatch {
  id: string;
  complementary_axis: string;
  queries: string[];
  citations: Array<{ label: string; url: string; excerpt: string | null }>;
  discovery_model_run_id: string;
  structuring_model_run_id: string;
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

export interface DiscoveryMergeStats {
  raw_batch_count: number;
  raw_candidate_count: number;
  consolidated_candidate_count: number;
  unique_publication_count: number;
  duplicate_publication_occurrence_count: number;
}

export interface DiscoveryResult {
  batches: DiscoveryBatch[];
  candidates: CandidateTopic[];
  total: number;
  merge_stats: DiscoveryMergeStats;
  warning: string;
}

export interface DiscoveryLaunchResult {
  job_id: string;
  status: string;
  reused: boolean;
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
  batch_id: string;
  reused: boolean;
  source_mode: "manual_import";
  subject_count: number;
  publication_count: number;
}

export function launchDiscovery(
  editionId: string,
  complementaryAxis: string,
  confirmNewResearch = false,
): Promise<DiscoveryLaunchResult> {
  return request(`/api/editions/${encodeURIComponent(editionId)}/discovery`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      complementary_axis: complementaryAxis,
      confirm_new_research: confirmNewResearch,
    }),
  });
}

export function retryDiscoveryStructuring(
  editionId: string,
  researchModelRunId: string,
  complementaryAxis: string,
): Promise<DiscoveryLaunchResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/reports/reprocess`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        research_model_run_id: researchModelRunId,
        complementary_axis: complementaryAxis,
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
): Promise<DiscoveryLaunchResult> {
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
): Promise<DiscoveryLaunchResult> {
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
): Promise<DiscoveryLaunchResult> {
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
  complementaryAxis: string = "manual-import",
  sensitivity: string = "internal",
): Promise<DiscoveryImportConfirmResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/import/confirm`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        markdown,
        expected_sha256: expectedSha256,
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

export function markDiscoverySource(
  editionId: string,
  sourceId: string,
  status: SourceVerificationStatus,
): Promise<SourceCandidate> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/discovery/sources/${encodeURIComponent(sourceId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
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
  throw new ApiError(
    typeof detail === "object" && detail?.message
      ? detail.message
      : "La découverte n’a pas pu être effectuée.",
    typeof detail === "object" && detail?.code
      ? detail.code
      : "discovery_error",
    response.status,
  );
}
