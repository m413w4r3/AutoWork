import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import {
  investigationRequest,
  type AnalystInvestigationStatus,
  type BudgetDimension,
  type BudgetDimensionView,
  type InvestigationStopReason,
  type ToolInvocationStatus,
} from "./investigation";

export interface ArticleClaimSource {
  id: string;
  statement: string;
  source_refs: string[];
}

export interface OperatorContextSectionPage {
  schema: "investigation-operator-context-section.v1";
  investigation_id: string;
  section: "article_claims";
  offset: number;
  limit: number;
  total: number;
  next_offset: number | null;
  items: ArticleClaimSource[];
}

export interface InvestigationActivityObservation {
  id: string;
  kind: string;
  sha256: string;
  source: string;
  source_version: string;
  observed_at: string;
}

export interface InvestigationActivityInvocation {
  id: string;
  version: number;
  tool_name: string;
  schema_version: string;
  purpose_excerpt: string;
  status: ToolInvocationStatus;
  created_at: string;
  updated_at: string;
  duration_seconds: number | null;
  duration_quality: "unknown";
  reserved_budget: Partial<Record<BudgetDimension, number>>;
  consumed_budget: Partial<Record<BudgetDimension, number>>;
  repeated_input_count_in_page: number;
  observation_refs: string[];
  observations: InvestigationActivityObservation[];
}

export type InvestigationActivityBlockerKind =
  | "permission_missing"
  | "service_unavailable"
  | "external_effect_ambiguous"
  | "budget_limit"
  | "evidence_insufficient"
  | "analytical_limitation"
  | "policy_block"
  | "tool_not_executable"
  | "technical_block";

export interface InvestigationActivityBlocker {
  kind: InvestigationActivityBlockerKind;
  code: string;
  invocation_id: string | null;
  external_effect_state: string | null;
  message: string;
  next_action: string;
}

export interface InvestigationActivityPage {
  schema_version: "investigation-activity.v1";
  investigation: {
    id: string;
    subject_id: string;
    production_run_id: string;
    synthesis_artifact_id: string;
    synthesis_sha256: string | null;
    status: AnalystInvestigationStatus;
    version: number;
    stop_reason: InvestigationStopReason | null;
    execution_mode: "operator_led" | "bridge_led" | null;
  };
  operator: {
    principal: string | null;
    model: string | null;
    effort: string | null;
    active_subagent_count: number | null;
    metadata_source: "unavailable_in_autowork";
  };
  working_state: {
    version: number;
    turn_index: number;
    stop_reason: InvestigationStopReason | null;
    hypothesis_count: number;
    open_hypothesis_count: number;
  } | null;
  last_activity_at: string | null;
  last_activity_source: string | null;
  active_backend_job_count: number;
  budget: Record<BudgetDimension, BudgetDimensionView> | null;
  timeline: InvestigationActivityInvocation[];
  next_cursor: string | null;
  has_more: boolean;
  blockers: InvestigationActivityBlocker[];
  counts: { inconclusive_findings: number };
  generated_at: string;
}

export const investigationOperatorQueryKeys = {
  activity: (investigationId: string) =>
    ["investigation-activity", investigationId, "latest"] as const,
  activityHistory: (investigationId: string) =>
    ["investigation-activity", investigationId, "history"] as const,
  articleClaims: (investigationId: string) =>
    ["investigation-operator-article-claims", investigationId] as const,
};

export function getInvestigationActivity(
  investigationId: string,
  cursor: string | null,
): Promise<InvestigationActivityPage> {
  const query = new URLSearchParams({ limit: "20" });
  if (cursor) query.set("cursor", cursor);
  return investigationRequest(
    `/api/investigations/${encodeURIComponent(investigationId)}/activity?${query.toString()}`,
  );
}

export function useInvestigationActivity(investigationId: string) {
  return useQuery({
    queryKey: investigationOperatorQueryKeys.activity(investigationId),
    queryFn: () => getInvestigationActivity(investigationId, null),
    enabled: Boolean(investigationId),
    refetchInterval: (query) => {
      const status = query.state.data?.investigation.status;
      if (status === "queued") return 5_000;
      if (status === "running") return 10_000;
      return false;
    },
    refetchIntervalInBackground: false,
  });
}

export function useInvestigationActivityHistory(
  investigationId: string,
  cursor: string | null,
  enabled: boolean,
) {
  return useInfiniteQuery({
    queryKey: investigationOperatorQueryKeys.activityHistory(investigationId),
    queryFn: ({ pageParam }) => getInvestigationActivity(investigationId, pageParam),
    initialPageParam: cursor,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(investigationId && cursor && enabled),
    refetchOnWindowFocus: false,
  });
}

export function getArticleClaimSources(
  investigationId: string,
  offset: number,
): Promise<OperatorContextSectionPage> {
  const query = new URLSearchParams({
    offset: String(offset),
    limit: "20",
  });
  return investigationRequest(
    `/api/investigations/${encodeURIComponent(investigationId)}/operator/context/section/article_claims?${query.toString()}`,
  );
}

export function useArticleClaimSources(investigationId: string) {
  return useInfiniteQuery({
    queryKey: investigationOperatorQueryKeys.articleClaims(investigationId),
    queryFn: ({ pageParam }) =>
      getArticleClaimSources(investigationId, pageParam),
    initialPageParam: 0,
    getNextPageParam: (lastPage) => lastPage.next_offset ?? undefined,
    enabled: Boolean(investigationId),
  });
}
