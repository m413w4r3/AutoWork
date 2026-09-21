import { ApiError } from "./editions";

export type SelectionState = "undecided" | "ignored" | "selected";
export type SelectionDecision = "select" | "ignore";

export interface SelectionPublication {
  title: string;
  url: string;
  publisher: string | null;
  role: string;
  published_at: string | null;
}

export interface SelectionProvisionalIoc {
  raw_value: string;
  normalized_value: string | null;
  proposed_type: string;
  declared_type: string | null;
  warnings: string[];
}

export interface SelectionLastDecision {
  id: string;
  decision: SelectionDecision;
  created_at?: string;
  actor_id?: string | null;
}

export interface SelectionItem {
  discovery_subject_id: string;
  title: string;
  summary: string | null;
  presentation: string | null;
  actor_or_campaign: string | null;
  publications: SelectionPublication[];
  candidate_count: number;
  technical_potential: number | null;
  technical_potential_reason: string | null;
  announced_artifacts: string[];
  artifacts?: string[];
  publisher_ioc_count_total: number | null;
  publisher_ioc_counts: number[];
  provisional_ioc_count: number;
  provisional_ioc_type_counts: Record<string, number>;
  provisional_iocs: SelectionProvisionalIoc[];
  uncertainties: string[];
  recommendation: string | null;
  state: SelectionState;
  subject_id: string | null;
  last_decision: SelectionLastDecision | null;
  updated_since_decision: boolean;
  selectable: boolean;
  blocking_reason: string | null;
}

export interface SelectionCounts {
  undecided: number;
  ignored: number;
  selected: number;
  total?: number;
}

export interface SelectionBoard {
  edition_id: string;
  snapshot_id: string | null;
  snapshot_version: number;
  counts: SelectionCounts;
  items: SelectionItem[];
  recommendation: string | null;
}

export interface SelectionDecisionInput {
  discovery_subject_id: string;
  decision: SelectionDecision;
  expected_decision_id: string | null;
}

export interface SelectionDecisionRequest {
  snapshot_version: number;
  decisions: SelectionDecisionInput[];
}

export function fetchSelectionBoard(
  editionId: string,
): Promise<SelectionBoard> {
  return request<SelectionBoard>(selectionUrl(editionId));
}

export function confirmSelectionDecisions(
  editionId: string,
  payload: SelectionDecisionRequest,
  idempotencyKey: string,
): Promise<SelectionBoard> {
  return request<SelectionBoard>(selectionUrl(editionId, "/decisions"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(payload),
  });
}

function selectionUrl(editionId: string, suffix = ""): string {
  return `/api/editions/${encodeURIComponent(editionId)}/selection${suffix}`;
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
      : detail?.message || "La sélection n’a pas pu être enregistrée.";
  const code =
    typeof detail === "object" && detail?.code
      ? detail.code
      : "selection_error";
  throw new ApiError(message, code, response.status);
}
