import { ApiError } from "./editions";

/** Read model of `GET /api/editions/{edition_id}/fusion`.
 *
 * Every identity is a business UUID: `DiscoveryCandidate.id` for candidates and
 * `discovery_subject_id` for discovery groups (not an editorial `Subject.id`).
 * Planner handles never cross this boundary. */

export interface FusionPublication {
  id: string;
  url: string;
  canonical_url: string;
  title: string;
  publisher: string;
  published_at: string | null;
}

export interface FusionCandidate {
  id: string;
  discovery_run_id: string;
  discovery_batch_id: string;
  supersedes_candidate_id: string | null;
  title: string;
  summary: string;
  event_date: string | null;
  actors: string[];
  campaigns: string[];
  malware: string[];
  cves: string[];
  iocs: string[];
  countries: string[];
  sectors: string[];
  likely_artifacts: string[];
  publications: FusionPublication[];
}

export interface FusionSignal {
  kind: string;
  value: string;
  candidate_ids: string[];
}

export interface FusionModelSuggestion {
  recommendation: string;
  summary: string;
}

export interface FusionHistoryEntry {
  action: string;
  merge_run_id: string | null;
  planner_kind: string | null;
  actor_id: string | null;
  candidate_ids: string[];
  created_at: string;
}

export interface FusionGroup {
  discovery_subject_id: string;
  title: string;
  summary: string;
  candidate_ids: string[];
  candidates: FusionCandidate[];
  confidence: string | null;
  origin: string | null;
  resolution_state: string;
  deterministic_signals: FusionSignal[];
  model_suggestion: FusionModelSuggestion | null;
  differences: string[];
  history: FusionHistoryEntry[];
}

export interface FusionReviewGroup {
  candidate_ids: string[];
  candidates: FusionCandidate[];
  proposed_discovery_subject_ids: string[];
  confidence: string;
  requires_decision: boolean;
  deterministic_signals: FusionSignal[];
  model_suggestion: FusionModelSuggestion | null;
  differences: string[];
}

export interface FusionPendingReview {
  merge_run_id: string;
  candidate_ids: string[];
  discovery_subject_ids: string[];
  review_reasons: string[];
  stale: boolean;
  groups: FusionReviewGroup[];
  created_at: string;
}

export interface FusionBoard {
  edition_id: string;
  snapshot_id: string | null;
  snapshot_version: number | null;
  read_only: boolean;
  candidate_count: number;
  group_count: number;
  pending_review_count: number;
  groups: FusionGroup[];
  pending_reviews: FusionPendingReview[];
  unstabilized_candidates: FusionCandidate[];
}

export type FusionReviewAction = "accept" | "separate" | "attach" | "defer";

export interface FusionReviewDecision {
  action: FusionReviewAction;
  candidate_ids: string[];
  target_discovery_subject_id?: string;
}

export interface FusionReviewResolution {
  snapshot_version: number;
  decisions: FusionReviewDecision[];
}

export interface FusionMergeRequest {
  snapshot_version: number;
  discovery_subject_ids: string[];
}

export interface FusionSplitRequest {
  snapshot_version: number;
  discovery_subject_id: string;
  candidate_ids: string[];
}

export const FUSION_SNAPSHOT_STALE = "fusion_snapshot_stale";

function fusionUrl(editionId: string, suffix = ""): string {
  return `/api/editions/${encodeURIComponent(editionId)}/fusion${suffix}`;
}

export function fetchFusionBoard(editionId: string): Promise<FusionBoard> {
  return request<FusionBoard>(fusionUrl(editionId));
}

export function resolveFusionReview(
  editionId: string,
  mergeRunId: string,
  payload: FusionReviewResolution,
): Promise<FusionBoard> {
  return post(
    fusionUrl(editionId, `/reviews/${encodeURIComponent(mergeRunId)}/resolve`),
    payload,
  );
}

export function mergeFusionSubjects(
  editionId: string,
  payload: FusionMergeRequest,
): Promise<FusionBoard> {
  return post(fusionUrl(editionId, "/merge"), payload);
}

export function splitFusionSubject(
  editionId: string,
  payload: FusionSplitRequest,
): Promise<FusionBoard> {
  return post(fusionUrl(editionId, "/split"), payload);
}

function post(url: string, payload: unknown): Promise<FusionBoard> {
  return request<FusionBoard>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (response.ok) return (await response.json()) as T;
  const body = (await response.json().catch(() => null)) as {
    detail?: { code?: string; message?: string } | string;
  } | null;
  const detail = body?.detail;
  const message =
    typeof detail === "object" && detail?.message
      ? detail.message
      : "L’opération de fusion n’a pas pu être effectuée.";
  const code =
    typeof detail === "object" && detail?.code ? detail.code : "fusion_error";
  throw new ApiError(message, code, response.status);
}
