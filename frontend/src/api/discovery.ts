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
  archived_report_url: string;
}

export interface DiscoveryResult {
  batches: DiscoveryBatch[];
  candidates: CandidateTopic[];
  total: number;
  warning: string;
}

export interface DiscoveryLaunchResult {
  job_id: string;
  status: string;
  reused: boolean;
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
