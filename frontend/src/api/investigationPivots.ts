import { useInfiniteQuery } from "@tanstack/react-query";

import { investigationRequest } from "./investigation";

export type PivotQualificationLevel =
  | "documented"
  | "discovered"
  | "candidate"
  | "associated"
  | "rejected"
  | "confirmed";

export type PivotExecutionState =
  "queued" | "executed" | "exhausted" | "blocked";

export type PivotG2State =
  "required" | "approved" | "blocked" | "not_applicable";

export type PivotDisclosureState =
  "allowed" | "blocked" | "unknown" | "not_applicable";

export interface PivotPriorityFactors {
  novelty_vs_article: number;
  novelty_vs_verified: number;
  depth: number;
  cost: number;
  formula: string;
}

export interface PivotFrontierNode {
  id: string;
  canonical_id: string;
  indicator_type: string;
  value: string;
  parent_id: string | null;
  path: string[];
  source_observation_id: string | null;
  source: string;
  source_version: string;
  source_sha256: string;
  level: PivotQualificationLevel;
  reason: string;
  confidence: number;
  limitations: string[];
  novelty_vs_article: boolean;
  novelty_vs_verified: boolean;
  estimated_cost: number;
  g2_state: PivotG2State;
  disclosure_state: PivotDisclosureState;
  priority_score: number;
  priority_factors: PivotPriorityFactors;
  state: PivotExecutionState;
  state_reason: string;
  cursor: string | null;
  pagination_complete: boolean | null;
  children_observed: string[];
}

export interface PivotFrontierEdge {
  id: string;
  parent_id: string;
  child_id: string;
  relation: string;
  source_observation_id: string;
  source_version: string;
  source_sha256: string;
  reason: string;
  confidence: number;
  limitations: string[];
}

export interface PivotFrontierPath {
  id: string;
  root_id: string;
  target_id: string;
  node_ids: string[];
  edge_ids: string[];
}

export interface PivotFrontierPage {
  schema_version: "investigation-pivot-frontier.v1";
  investigation_id: string;
  source_sha256: string;
  frontier_sha256: string;
  nodes: PivotFrontierNode[];
  edges: PivotFrontierEdge[];
  paths: PivotFrontierPath[];
  truncated: boolean;
  next_cursor: string | null;
}

export const investigationPivotQueryKeys = {
  frontier: (investigationId: string) =>
    ["investigation-pivot-frontier", investigationId] as const,
};

export function getInvestigationPivotFrontier(
  investigationId: string,
  cursor: string | null = null,
): Promise<PivotFrontierPage> {
  const query = new URLSearchParams({ limit: "200" });
  if (cursor) query.set("cursor", cursor);
  return investigationRequest(
    `/api/investigations/${encodeURIComponent(investigationId)}/pivots?${query.toString()}`,
  );
}

export function useInvestigationPivotFrontier(investigationId: string) {
  return useInfiniteQuery({
    queryKey: investigationPivotQueryKeys.frontier(investigationId),
    queryFn: ({ pageParam }) =>
      getInvestigationPivotFrontier(investigationId, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) =>
      lastPage.truncated ? (lastPage.next_cursor ?? undefined) : undefined,
    enabled: Boolean(investigationId),
  });
}
