import { useQuery } from "@tanstack/react-query";

import { investigationRequest } from "./investigation";

export type ExperimentVerdict =
  "supports" | "refutes" | "qualifies" | "inconclusive" | "blocked";

export type ConsolidatedClaimStatus =
  "supported" | "refuted" | "qualified" | "inconclusive";

export interface ExperimentInputRef {
  input_type: string;
  input_id: string;
  sha256: string | null;
}

export interface ExperimentInvocationRef {
  invocation_id: string;
  version: number | "[redacted]";
}

export interface ExperimentObservationRef {
  observation_id: string;
  sha256: string;
}

export interface InvestigationExperiment {
  schema_version: "investigation-experiment.v1";
  id: string;
  investigation_id: string;
  version: number;
  hypothesis_id: string;
  claim_ids: string[];
  working_state_version: number;
  g2_envelope_version: number;
  prediction: string;
  falsification_criterion: string;
  method: string;
  method_mode: "passive";
  input_type: string;
  input_refs: ExperimentInputRef[];
  preconditions: string[];
  availability: "available" | "limited" | "unavailable";
  tool_name: string;
  tool_version: string;
  tool_schema_version: string;
  tool_arguments: Record<string, unknown>;
  step_ids: string[];
  tool_invocation_refs: ExperimentInvocationRef[];
  observation_refs: ExperimentObservationRef[];
  expected: string;
  actual: string;
  actual_outcome: "observed" | "not_observed" | "unknown";
  observation_coverage: "complete" | "limited" | "none";
  started_at: string;
  completed_at: string;
  recorded_at: string;
  scope: string;
  limitations: string[];
  verdict: ExperimentVerdict;
  /** An explicit warning that `refutes` is a stored assertion, not a conclusion. */
  verdict_assertion_label?: string | null;
  /** Additive fields; absent on legacy records and older timeline responses. */
  supersedes_experiment_ids?: string[];
  superseded_by?: string[];
  implication: string;
  next_pivots: string[];
  author: string;
  origin: string;
}

export interface ClaimExperimentScope {
  experiment_id: string;
  scope: string;
  verdict: ExperimentVerdict;
  observation_ids: string[];
  superseded_by?: string[];
}

export interface ConsolidatedClaimVerdict {
  claim_id: string;
  status: ConsolidatedClaimStatus;
  verdicts: ExperimentVerdict[];
  experiment_ids: string[];
  scopes: ClaimExperimentScope[];
  contradictory: boolean;
  superseded_by?: string[];
}

export interface InvestigationExperimentTimeline {
  schema_version: "investigation-experiment-timeline.v1";
  investigation_id: string;
  stream_version: number;
  experiments: InvestigationExperiment[];
  claims: ConsolidatedClaimVerdict[];
}

export const investigationExperimentQueryKeys = {
  timeline: (investigationId: string) =>
    ["investigation-experiment-timeline", investigationId] as const,
};

export function getInvestigationExperiments(
  investigationId: string,
): Promise<InvestigationExperimentTimeline> {
  return investigationRequest(
    `/api/investigations/${encodeURIComponent(investigationId)}/experiments`,
  );
}

export function useInvestigationExperiments(investigationId: string) {
  return useQuery({
    queryKey: investigationExperimentQueryKeys.timeline(investigationId),
    queryFn: () => getInvestigationExperiments(investigationId),
    enabled: Boolean(investigationId),
  });
}
