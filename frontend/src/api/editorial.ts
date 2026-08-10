import { ApiError } from "./editions";

export type EditorialType = "brief" | "major";
export type EditorialGroupStatus =
  "proposed" | "rejected" | "selected" | "superseded";
export type GroupingOutcome =
  | "new_subject"
  | "duplicate_same_publication"
  | "update_previous_subject"
  | "non_independent_reprint"
  | "ambiguous_review";

export interface EditorialCandidate {
  id: string;
  batch_id: string;
  title: string;
  summary: string;
  event_date: string | null;
  source_urls: string[];
}

export interface EditorialScore {
  impact: number;
  novelty: number;
  technical_depth: number;
  hunting_potential: number;
  actionability: number;
  source_quality: number;
  total: number;
  justifications: Record<string, string>;
}

export interface EditorialGroup {
  id: string;
  edition_id: string;
  title: string;
  outcome: GroupingOutcome;
  status: EditorialGroupStatus;
  editorial_type: EditorialType | null;
  subject_id: string | null;
  candidates: EditorialCandidate[];
  score: EditorialScore;
  source_relationship_status: "provisional" | "verified";
  needs_source_verification: boolean;
  needs_source_expansion: boolean;
  grouping_confidence: "low" | "medium" | "high";
  grouping_justification: string;
  historical_comparison: {
    group_id: string;
    title: string;
    editorial_type: EditorialType | null;
    subject_id: string | null;
  } | null;
  version: number;
}

export interface EditorialBoardResult {
  groups: EditorialGroup[];
  selected_briefs: number;
  selected_major: number;
  target_briefs: number;
  target_major: number;
  automatic_selection: false;
}

export function fetchEditorialBoard(
  editionId: string,
): Promise<EditorialBoardResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/editorial-groups`,
  );
}

export function mergeEditorialGroups(
  editionId: string,
  groupIds: string[],
): Promise<EditorialBoardResult> {
  return mutate(editionId, "/merge", { group_ids: groupIds });
}

export function splitEditorialGroup(
  editionId: string,
  groupId: string,
  candidateIds: string[],
): Promise<EditorialBoardResult> {
  return mutate(editionId, `/${encodeURIComponent(groupId)}/split`, {
    candidate_ids: candidateIds,
  });
}

export function rejectEditorialGroup(
  editionId: string,
  groupId: string,
  reason: string,
): Promise<EditorialBoardResult> {
  return mutate(editionId, `/${encodeURIComponent(groupId)}/reject`, {
    reason,
  });
}

export function selectEditorialGroup(
  editionId: string,
  groupId: string,
  editorialType: EditorialType,
): Promise<EditorialBoardResult> {
  return mutate(editionId, `/${encodeURIComponent(groupId)}/select`, {
    editorial_type: editorialType,
  });
}

function mutate(
  editionId: string,
  suffix: string,
  payload: object,
): Promise<EditorialBoardResult> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/editorial-groups${suffix}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
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
  const message =
    typeof detail === "string"
      ? detail
      : detail?.message || "L’action éditoriale n’a pas pu être effectuée.";
  throw new ApiError(
    message,
    typeof detail === "object" && detail?.code
      ? detail.code
      : "editorial_action_error",
    response.status,
  );
}
