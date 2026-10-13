import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export type AnalystInvestigationStatus =
  | "queued"
  | "running"
  | "awaiting_review"
  | "completed"
  | "exhausted"
  | "failed"
  | "cancelled";

export type InvestigationStopReason =
  | "agent_finished"
  | "budget_exhausted"
  | "no_new_information"
  | "user_interrupt"
  | "model_unavailable"
  | "permission_required"
  | "submission_needs_review";

export type FindingKind =
  | "new_sample"
  | "infection_chain"
  | "capability"
  | "network_behaviour"
  | "claim_verification"
  | "detection";

export type FindingAssessment =
  "supported" | "refuted" | "qualified" | "inconclusive";

export type FindingNovelty =
  | "new_to_article"
  | "already_documented_but_new_detail"
  | "claim_correction"
  | "not_confirmed";

export type EvidenceKind =
  | "vt_file_report"
  | "vt_relationship"
  | "vt_behaviour"
  | "shodan_observation"
  | "binary_analysis"
  | "binary_string"
  | "yara_match"
  | "negative_result"
  | "claim_source"
  | "other";

export type SampleOrigin =
  "seed" | "vt_pivot" | "relationship" | "behavior" | "manual";

export type SampleQualification =
  "candidate" | "related" | "confirmed" | "rejected" | "uncertain";

export type BudgetDimension =
  | "bridge_turns"
  | "vt_units"
  | "shodan_credits"
  | "downloads"
  | "tool_seconds"
  | "context_tokens"
  | "context_bytes";

export type ToolInvocationStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "empty"
  | "unavailable"
  | "policy_denied"
  | "needs_review"
  | "failed"
  | "partial";

export interface InvestigationAgendaItem {
  question: string;
  potential: number;
  rank: number;
}

export type HypothesisStatus = "open" | "supported" | "refuted" | "abandoned";

export interface InvestigationHypothesis {
  id: string;
  statement: string;
  status: HypothesisStatus;
  evidence_refs: string[];
  finding_refs: string[];
}

export interface InvestigationFinding {
  id: string;
  version: number;
  kind: FindingKind;
  assessment: FindingAssessment;
  novelty: FindingNovelty | null;
  statement: string;
  scope: string;
  limitations: string[];
  observation_refs: string[];
  claim_refs: string[];
  created_at: string;
}

export interface InvestigationObservation {
  id: string;
  kind: EvidenceKind;
  sha256: string;
  source: string;
  source_version: string;
  observed_at: string;
  object_type: string;
  object_id: string;
  limitations: string[];
  invocation_ref: string | null;
}

export interface InvestigationSampleAssociation {
  id: string;
  sample_sha256: string;
  origin: SampleOrigin;
  qualification: SampleQualification;
  related_sample_sha256: string[];
  version: number;
}

export type OpinionBlockType =
  | "key_findings"
  | "additional_samples"
  | "infection_chain_and_capabilities"
  | "network_and_infrastructure"
  | "claim_verification"
  | "proposed_detections"
  | "uncertainties_and_next_leads";

export type OpinionQualification = "fact" | "inference" | "hypothesis";

export interface InvestigationOpinionBlock {
  type: OpinionBlockType;
  text: string;
  finding_refs: string[];
  evidence_refs: string[];
  qualification: OpinionQualification;
  certainty: number;
}

export interface InvestigationOpinionDraft {
  id: string;
  version: number;
  status: "draft" | "approved" | "rejected";
  content_sha256: string;
  blocks: InvestigationOpinionBlock[];
  unresolved_points: string[];
  created_at: string;
}

export interface BudgetDimensionView {
  limit: number | null;
  consumed: number;
  remaining: number | null;
}

export interface BlockedInvestigationDecision {
  code: string;
  tool_name: string | null;
}

export interface InvestigationView {
  id: string;
  subject_id: string;
  production_run_id: string;
  synthesis_artifact_id: string;
  synthesis_sha256: string;
  status: AnalystInvestigationStatus;
  version: number;
  stop_reason: InvestigationStopReason | null;
  agenda: InvestigationAgendaItem[];
  hypotheses: InvestigationHypothesis[];
  findings: InvestigationFinding[];
  observations: InvestigationObservation[];
  sample_associations: InvestigationSampleAssociation[];
  opinion_draft: InvestigationOpinionDraft | null;
  budget: Record<BudgetDimension, BudgetDimensionView>;
  blocked_decisions: BlockedInvestigationDecision[];
  available_tools: string[];
  unavailable_tools: string[];
  envelope_approved: boolean;
  allowed_providers: string[];
  acquire_hits_allowed: boolean;
}

export interface InvestigationSummary extends Pick<
  InvestigationView,
  | "id"
  | "subject_id"
  | "production_run_id"
  | "synthesis_artifact_id"
  | "synthesis_sha256"
  | "status"
  | "version"
  | "stop_reason"
> {
  updated_at: string;
}

export interface InvestigationActionResponse {
  investigation: InvestigationView;
  job_id: string | null;
  idempotent_replay: boolean;
}

export interface InvestigationStartInput {
  production_run_id: string;
  synthesis_artifact_id: string;
  synthesis_sha256: string;
}

export interface InvestigationActionInput {
  expected_version: number;
}

export interface CitationViolationPayload {
  code: string;
  block_type: string;
  reference_id: string | null;
  message: string;
}

export class InvestigationApiError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status: number,
    public readonly details: Record<string, unknown> | null = null,
  ) {
    super(message);
    this.name = "InvestigationApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function investigationRequest<T>(
  url: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(url, init);
  if (response.ok) {
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  const body: unknown = await response.json().catch(() => null);
  const detail = isRecord(body) ? body.detail : undefined;
  const detailRecord = isRecord(detail) ? detail : null;
  const code =
    typeof detailRecord?.code === "string"
      ? detailRecord.code
      : "investigation_api_error";
  const message =
    typeof detail === "string"
      ? detail
      : typeof detailRecord?.message === "string"
        ? detailRecord.message
        : `La requête d’enquête a échoué (${code}).`;
  throw new InvestigationApiError(message, code, response.status, detailRecord);
}

export function createIdempotencyKey(): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  return (
    randomUuid ?? `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`
  );
}

function postJson<T>(url: string, payload: unknown, idempotencyKey: string) {
  return investigationRequest<T>(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(payload),
  });
}

export const investigationQueryKeys = {
  subject: (subjectId: string) =>
    ["subject-investigations", subjectId] as const,
  detail: (investigationId: string) =>
    ["investigation", investigationId] as const,
};

export function listSubjectInvestigations(
  subjectId: string,
): Promise<InvestigationSummary[]> {
  return investigationRequest(
    `/api/subjects/${encodeURIComponent(subjectId)}/investigations`,
  );
}

export function getInvestigation(
  investigationId: string,
): Promise<InvestigationView> {
  return investigationRequest(
    `/api/investigations/${encodeURIComponent(investigationId)}`,
  );
}

export function startInvestigation(
  subjectId: string,
  input: InvestigationStartInput,
  idempotencyKey: string,
): Promise<InvestigationActionResponse> {
  return postJson(
    `/api/subjects/${encodeURIComponent(subjectId)}/investigations`,
    input,
    idempotencyKey,
  );
}

export function resumeInvestigation(
  investigationId: string,
  input: InvestigationActionInput,
  idempotencyKey: string,
): Promise<InvestigationActionResponse> {
  return postJson(
    `/api/investigations/${encodeURIComponent(investigationId)}/resume`,
    input,
    idempotencyKey,
  );
}

export function interruptInvestigation(
  investigationId: string,
  input: InvestigationActionInput,
  idempotencyKey: string,
): Promise<InvestigationActionResponse> {
  return postJson(
    `/api/investigations/${encodeURIComponent(investigationId)}/interrupt`,
    input,
    idempotencyKey,
  );
}

export function useSubjectInvestigations(subjectId: string) {
  return useQuery({
    queryKey: investigationQueryKeys.subject(subjectId),
    queryFn: () => listSubjectInvestigations(subjectId),
    enabled: Boolean(subjectId),
  });
}

export function useInvestigation(investigationId: string | null) {
  return useQuery({
    queryKey: investigationQueryKeys.detail(investigationId ?? "none"),
    queryFn: () => getInvestigation(investigationId ?? ""),
    enabled: Boolean(investigationId),
    refetchInterval: (query) =>
      query.state.data?.status === "queued" ||
      query.state.data?.status === "running"
        ? 2_000
        : false,
  });
}

export function useStartInvestigation(subjectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: InvestigationStartInput) =>
      startInvestigation(subjectId, input, createIdempotencyKey()),
    onSuccess: (result) => {
      queryClient.setQueryData(
        investigationQueryKeys.detail(result.investigation.id),
        result.investigation,
      );
      void queryClient.invalidateQueries({
        queryKey: investigationQueryKeys.subject(subjectId),
      });
    },
  });
}

function useInvestigationAction(
  action: typeof resumeInvestigation | typeof interruptInvestigation,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (
      input: { investigationId: string } & InvestigationActionInput,
    ) =>
      action(
        input.investigationId,
        { expected_version: input.expected_version },
        createIdempotencyKey(),
      ),
    onSuccess: (result) => {
      queryClient.setQueryData(
        investigationQueryKeys.detail(result.investigation.id),
        result.investigation,
      );
      void queryClient.invalidateQueries({
        queryKey: investigationQueryKeys.detail(result.investigation.id),
      });
      void queryClient.invalidateQueries({
        queryKey: investigationQueryKeys.subject(
          result.investigation.subject_id,
        ),
      });
    },
  });
}

export function useResumeInvestigation() {
  return useInvestigationAction(resumeInvestigation);
}

export function useInterruptInvestigation() {
  return useInvestigationAction(interruptInvestigation);
}
