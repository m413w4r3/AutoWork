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
  snapshot_version: number | null;
  counts: SelectionCounts;
  items: SelectionItem[];
  recommendation: string | null;
  fusion_review_count: number;
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

/*
 * Wire contract — mirrors `backend/src/cti_app/api/selection.py` field by
 * field. The board above is the UI model: the two schemas differ, so every
 * response goes through `normalizeSelectionBoard` instead of a cast.
 */

interface SelectionPublicationWire {
  url: string;
  title: string;
  publisher: string | null;
  role: string;
  published_at: string | null;
  ioc_declared_count: number | null;
  ioc_visible_count: number | null;
}

interface SelectionProvisionalIocWire {
  raw_value: string;
  normalized_value: string | null;
  declared_type: string | null;
  proposed_type: string;
  warnings: string[];
}

interface SelectionLastDecisionWire {
  id: string;
  action: SelectionDecision;
  snapshot_id: string;
  snapshot_version: number;
  subject_id: string | null;
  actor_id: string;
  occurred_at: string;
}

interface SelectionItemWire {
  discovery_subject_id: string;
  canonical_discovery_subject_id: string;
  title: string;
  summary: string;
  actor_or_campaign: string;
  technical_potential: number;
  technical_potential_reason: string;
  artifacts: string[];
  publications: SelectionPublicationWire[];
  provisional_iocs: SelectionProvisionalIocWire[];
  uncertainties: string[];
  selectable: boolean;
  blocking_reason: string | null;
  effective_state: SelectionState;
  subject_id: string | null;
  recommendation: { recommended: boolean; reason: string | null };
  last_decision: SelectionLastDecisionWire | null;
  updated_since_decision: boolean;
  member_candidate_ids: string[];
}

interface SelectionBoardWire {
  edition_id: string;
  snapshot_id: string | null;
  snapshot_version: number | null;
  items: SelectionItemWire[];
  fusion_review_count: number;
  selected: number;
  ignored: number;
  undecided: number;
}

interface SelectionDecisionWireInput {
  discovery_subject_id: string;
  action: SelectionDecision;
  expected_decision_id: string | null;
}

const RECOMMENDATION_LABELS: Record<string, string> = {
  ioc_signal: "IOC repérés dans les publications.",
};

export function normalizeSelectionBoard(
  board: SelectionBoardWire,
): SelectionBoard {
  return {
    edition_id: board.edition_id,
    snapshot_id: board.snapshot_id,
    snapshot_version: board.snapshot_version,
    counts: {
      undecided: board.undecided,
      ignored: board.ignored,
      selected: board.selected,
      total: board.items.length,
    },
    items: board.items.map(normalizeSelectionItem),
    fusion_review_count: board.fusion_review_count,
    recommendation:
      board.fusion_review_count > 0
        ? `${board.fusion_review_count} fusion(s) à résoudre avant de décider.`
        : null,
  };
}

function normalizeSelectionItem(item: SelectionItemWire): SelectionItem {
  const declaredCounts = item.publications
    .map((publication) => publication.ioc_declared_count)
    .filter((count): count is number => count !== null);
  return {
    discovery_subject_id: item.discovery_subject_id,
    title: item.title,
    summary: item.summary || null,
    presentation: null,
    actor_or_campaign: item.actor_or_campaign || null,
    publications: item.publications.map((publication) => ({
      title: publication.title,
      url: publication.url,
      publisher: publication.publisher,
      role: publication.role,
      published_at: publication.published_at,
    })),
    candidate_count: item.member_candidate_ids.length,
    technical_potential: item.technical_potential,
    technical_potential_reason: item.technical_potential_reason || null,
    announced_artifacts: item.artifacts,
    publisher_ioc_count_total: declaredCounts.length
      ? declaredCounts.reduce((total, count) => total + count, 0)
      : null,
    publisher_ioc_counts: declaredCounts,
    provisional_ioc_count: item.provisional_iocs.length,
    provisional_ioc_type_counts: countIocTypes(item.provisional_iocs),
    provisional_iocs: item.provisional_iocs.map((ioc) => ({
      raw_value: ioc.raw_value,
      normalized_value: ioc.normalized_value,
      proposed_type: ioc.proposed_type,
      declared_type: ioc.declared_type,
      warnings: ioc.warnings,
    })),
    uncertainties: item.uncertainties,
    recommendation: item.recommendation.recommended
      ? (RECOMMENDATION_LABELS[item.recommendation.reason ?? ""] ??
        "Recommandé par la politique de sélection.")
      : null,
    state: item.effective_state,
    subject_id: item.subject_id,
    last_decision: item.last_decision
      ? {
          id: item.last_decision.id,
          decision: item.last_decision.action,
          created_at: item.last_decision.occurred_at,
          actor_id: item.last_decision.actor_id,
        }
      : null,
    updated_since_decision: item.updated_since_decision,
    selectable: item.selectable,
    blocking_reason: item.blocking_reason,
  };
}

function countIocTypes(
  iocs: readonly SelectionProvisionalIocWire[],
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const ioc of iocs) {
    counts[ioc.proposed_type] = (counts[ioc.proposed_type] ?? 0) + 1;
  }
  return counts;
}

export async function fetchSelectionBoard(
  editionId: string,
): Promise<SelectionBoard> {
  return normalizeSelectionBoard(
    await request<SelectionBoardWire>(selectionUrl(editionId)),
  );
}

export async function confirmSelectionDecisions(
  editionId: string,
  payload: SelectionDecisionRequest,
  idempotencyKey: string,
): Promise<SelectionBoard> {
  // The API names the verb `action`; the board keeps calling it a decision.
  const body: {
    snapshot_version: number;
    decisions: SelectionDecisionWireInput[];
  } = {
    snapshot_version: payload.snapshot_version,
    decisions: payload.decisions.map((decision) => ({
      discovery_subject_id: decision.discovery_subject_id,
      action: decision.decision,
      expected_decision_id: decision.expected_decision_id,
    })),
  };
  return normalizeSelectionBoard(
    await request<SelectionBoardWire>(selectionUrl(editionId, "/decisions"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(body),
    }),
  );
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
