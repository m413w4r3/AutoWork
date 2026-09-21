/*
 * Selection fixtures in the exact shape FastAPI serializes
 * (`backend/src/cti_app/api/selection.py`). E2E specs must feed the UI the
 * real wire contract — `effective_state`, `member_candidate_ids`, an object
 * recommendation, top-level counters — so that a divergence between the API
 * and the client fails here instead of passing against an invented payload.
 */

export type SelectionWireState = "undecided" | "ignored" | "selected";

export interface SelectionWireLastDecision {
  id: string;
  action: "select" | "ignore";
  snapshot_id: string;
  snapshot_version: number;
  subject_id: string | null;
  actor_id: string;
  occurred_at: string;
}

export interface SelectionWireItem {
  discovery_subject_id: string;
  canonical_discovery_subject_id: string;
  title: string;
  summary: string;
  actor_or_campaign: string;
  technical_potential: number;
  technical_potential_reason: string;
  artifacts: string[];
  publications: Array<Record<string, unknown>>;
  provisional_iocs: Array<Record<string, unknown>>;
  uncertainties: string[];
  selectable: boolean;
  blocking_reason: string | null;
  effective_state: SelectionWireState;
  subject_id: string | null;
  recommendation: { recommended: boolean; reason: string | null };
  last_decision: SelectionWireLastDecision | null;
  updated_since_decision: boolean;
  member_candidate_ids: string[];
}

export interface SelectionWireBoard {
  edition_id: string;
  snapshot_id: string | null;
  snapshot_version: number | null;
  items: SelectionWireItem[];
  fusion_review_count: number;
  selected: number;
  ignored: number;
  undecided: number;
}

export function selectionWireItem(
  overrides: Partial<SelectionWireItem> & {
    discovery_subject_id: string;
    title: string;
  },
): SelectionWireItem {
  const state = overrides.effective_state ?? "undecided";
  return {
    canonical_discovery_subject_id: overrides.discovery_subject_id,
    summary: `Présentation neutre de ${overrides.title}.`,
    actor_or_campaign: "Acteur à confirmer",
    technical_potential: 4,
    technical_potential_reason: "Artefacts techniques annoncés.",
    artifacts: ["ioc"],
    publications: [],
    provisional_iocs: [],
    uncertainties: [],
    selectable: true,
    blocking_reason: null,
    effective_state: state,
    subject_id: null,
    recommendation: { recommended: false, reason: null },
    last_decision: null,
    updated_since_decision: false,
    member_candidate_ids: [`${overrides.discovery_subject_id}-candidate`],
    ...overrides,
  };
}

export function selectionWireLastDecision(
  overrides: Partial<SelectionWireLastDecision> & { id: string },
): SelectionWireLastDecision {
  return {
    action: "select",
    snapshot_id: "66666666-6666-4666-8666-666666666661",
    snapshot_version: 1,
    subject_id: null,
    actor_id: "analyst",
    occurred_at: "2026-08-29T00:00:00Z",
    ...overrides,
  };
}

export function selectionWireBoard(
  overrides: Partial<SelectionWireBoard> & {
    edition_id: string;
    items: SelectionWireItem[];
  },
): SelectionWireBoard {
  const items = overrides.items;
  return {
    snapshot_id: "66666666-6666-4666-8666-666666666661",
    snapshot_version: 1,
    fusion_review_count: 0,
    selected: items.filter((item) => item.effective_state === "selected")
      .length,
    ignored: items.filter((item) => item.effective_state === "ignored").length,
    undecided: items.filter((item) => item.effective_state === "undecided")
      .length,
    ...overrides,
  };
}
