import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createIdempotencyKey,
  investigationRequest,
  investigationQueryKeys,
  type InvestigationApiError,
} from "./investigation";

export type DetectionCandidateStatus =
  "candidate" | "tested" | "rejected" | "approved" | "experimental";

export type DetectionTestRole =
  "confirmed_positive" | "out_of_cluster_candidate" | "goodware" | "holdout";
export type RuleValidationStatus =
  "untested" | "synthetic_only" | "real_partial" | "real_validated";

export interface DetectionTestCase {
  sha256: string;
  provenance: "real" | "synthetic";
  outcome: string;
}

export interface DetectionTestRoleReport {
  role: DetectionTestRole;
  corpus_sha256s: string[];
  provenance_counts?: { real: number; synthetic: number };
  test_cases?: DetectionTestCase[];
  matches: string[];
  non_matches: string[];
  non_measurable: string[];
  errors: Array<{
    code: string;
    sample_sha256: string;
    line: number | null;
  }>;
}

export interface DetectionTestReport {
  observation_id: string;
  sha256: string;
  source_sha256: string | null;
  corpus_sha256: string | null;
  corpus_sha256s_by_role: Partial<Record<DetectionTestRole, string[]>>;
  engine_version: string | null;
  execution_status: string | null;
  validation_status: RuleValidationStatus;
  selection_bias: string | null;
  coverage_statement: string | null;
  provenance_counts_by_role: Partial<
    Record<DetectionTestRole, { real: number; synthetic: number }>
  >;
  false_positive_count: number;
  false_positive_sha256s: string[];
  false_negative_count: number;
  false_negative_sha256s: string[];
  roles: DetectionTestRoleReport[];
  limitations: string[];
  known_false_positives: string[];
  known_false_positive_status: "present" | "none_observed" | "not_tested";
  goodware_case_count: number;
}

export interface DetectionDecisionView {
  id: string;
  type: string;
  actor: string;
  reason: string;
  occurred_at: string;
  correlation_id: string;
  known_limitations: string;
  source_sha256: string | null;
  test_report: Record<string, unknown> | null;
  base_publication: Record<string, unknown> | null;
}

export interface DetectionVersionReview {
  candidate_id: string;
  version_id: string;
  version: number;
  parent_version_id: string | null;
  investigation_id: string;
  source: string;
  source_sha256: string;
  status: DetectionCandidateStatus;
  validation_status: RuleValidationStatus;
  created_at: string;
  test_reports: DetectionTestReport[];
  latest_decision: DetectionDecisionView | null;
  comparisons: Record<string, unknown>[];
}

export interface InvestigationDetectionsResponse {
  investigation_id: string;
  versions: DetectionVersionReview[];
}

export interface DetectionReviewInput {
  version_id: string;
  expected_source_sha256: string;
  reason: string;
  known_limitations?: string;
  action: "approve" | "reject";
}

export interface DetectionDecisionResponse {
  status: "approved" | "rejected";
  decision: DetectionDecisionView;
}

export const detectionQueryKeys = {
  list: (investigationId: string) =>
    ["investigation-detections", investigationId] as const,
};

export function listInvestigationDetections(
  investigationId: string,
): Promise<InvestigationDetectionsResponse> {
  return investigationRequest(
    `/api/investigations/${encodeURIComponent(investigationId)}/detections`,
  );
}

export function reviewDetection(
  input: DetectionReviewInput,
  idempotencyKey: string,
): Promise<DetectionDecisionResponse> {
  const { action, version_id, ...review } = input;
  return investigationRequest(
    `/api/detections/${encodeURIComponent(version_id)}/${action}`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(review),
    },
  );
}

export function useInvestigationDetections(investigationId: string | null) {
  return useQuery({
    queryKey: detectionQueryKeys.list(investigationId ?? "none"),
    queryFn: () => listInvestigationDetections(investigationId ?? ""),
    enabled: Boolean(investigationId),
  });
}

export function useReviewDetection(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: DetectionReviewInput) =>
      reviewDetection(input, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: detectionQueryKeys.list(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: investigationQueryKeys.detail(investigationId),
      });
    },
  });
}

export type DetectionReviewError = InvestigationApiError;
