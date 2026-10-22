import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createIdempotencyKey,
  investigationQueryKeys,
  investigationRequest,
  type BudgetDimension,
  type InvestigationActionResponse,
  type InvestigationSampleAssociation,
  type InvestigationView,
} from "./investigation";

export type GovernanceTlpBound = "white" | "clear" | "green" | "amber" | "red";
export type GovernanceTlpInput = GovernanceTlpBound | "amber+strict";

export interface InvestigationEnvelopeDimensionView {
  limit: number | null;
  consumed: number;
  reserved: number;
  remaining: number | null;
}

export interface InvestigationEnvelopeView {
  investigation_id: string;
  approved: boolean;
  version: number;
  allowed_providers: string[];
  available_providers: string[];
  acquire_hits_allowed: boolean;
  max_acquire_volume: number;
  budget: Record<BudgetDimension, InvestigationEnvelopeDimensionView>;
  disclosure: {
    tlp: GovernanceTlpBound | null;
    do_not_submit: boolean;
    external_llm_allowed: boolean;
  };
  approval_decision_id: string | null;
  approval_actor_id: string | null;
  approved_at: string | null;
}

export interface EnvelopeDisclosureRequest {
  tlp?: GovernanceTlpInput | null;
  do_not_submit?: boolean | null;
  external_llm_allowed?: boolean | null;
}

export interface InvestigationEnvelopeDecisionInput {
  investigation_id: string;
  expected_version: number | null;
  allowed_providers: string[];
  budget_limits: Partial<Record<BudgetDimension, number>>;
  acquire_hits_allowed: boolean | null;
  max_acquire_volume: number | null;
  disclosure: EnvelopeDisclosureRequest;
  reason: string;
  correlation_id: string;
}

export interface InvestigationEnvelopeRevokeInput {
  investigation_id: string;
  expected_version: number;
  reason: string;
  correlation_id: string;
}

export type SampleQualificationState =
  "candidate" | "related" | "confirmed" | "rejected" | "uncertain";

export interface QualificationChangeView {
  from_qualification: SampleQualificationState;
  to_qualification: SampleQualificationState;
  actor_kind: "human" | "agent" | "system";
  actor_id: string;
  reason: string;
  changed_at: string;
}

export interface InvestigationSampleDetail extends InvestigationSampleAssociation {
  changes: QualificationChangeView[];
}

export interface InvestigationSamplesInput {
  investigation_id: string;
}

export interface SampleQualificationDecisionInput {
  investigation_id: string;
  association_id: string;
  expected_version: number;
  qualification: SampleQualificationState;
  reason: string;
  evidence_refs: string[];
  correlation_id: string;
}

export interface SampleQualificationResponse {
  association: InvestigationSampleDetail;
  idempotent_replay: boolean;
}

export type InvestigationRequestTargetKind = "hash" | "ioc" | "claim_id";

export interface InvestigationRequestTarget {
  kind: InvestigationRequestTargetKind;
  value: string;
}

export interface InvestigationUserRequestInput {
  investigation_id: string;
  text: string;
  target?: InvestigationRequestTarget | null;
  expected_version?: number | null;
}

export interface InvestigationUserRequestResponse {
  investigation: InvestigationView;
  request_id: string;
  job_id: string | null;
  idempotent_replay: boolean;
}

export interface InvestigationGovernanceClient {
  getEnvelope(investigationId: string): Promise<InvestigationEnvelopeView>;
  getSamples(investigationId: string): Promise<InvestigationSampleDetail[]>;
  approveEnvelope(
    input: InvestigationEnvelopeDecisionInput,
    idempotencyKey: string,
  ): Promise<InvestigationEnvelopeView>;
  extendEnvelope(
    input: InvestigationEnvelopeDecisionInput,
    idempotencyKey: string,
  ): Promise<InvestigationEnvelopeView>;
  revokeEnvelope(
    input: InvestigationEnvelopeRevokeInput,
    idempotencyKey: string,
  ): Promise<InvestigationEnvelopeView>;
  qualifySample(
    input: SampleQualificationDecisionInput,
    idempotencyKey: string,
  ): Promise<SampleQualificationResponse>;
  requestAnalysis(
    input: InvestigationUserRequestInput,
    idempotencyKey: string,
  ): Promise<InvestigationUserRequestResponse>;
}

function postJson<T>(
  url: string,
  payload: unknown,
  idempotencyKey: string,
): Promise<T> {
  return investigationRequest<T>(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(payload),
  });
}

function investigationUrl(investigationId: string): string {
  return `/api/investigations/${encodeURIComponent(investigationId)}`;
}

export const investigationGovernanceClient: InvestigationGovernanceClient = {
  getEnvelope(investigationId) {
    return investigationRequest(
      `${investigationUrl(investigationId)}/envelope`,
    );
  },
  getSamples(investigationId) {
    return investigationRequest(`${investigationUrl(investigationId)}/samples`);
  },
  approveEnvelope(input, idempotencyKey) {
    return postJson(
      `${investigationUrl(input.investigation_id)}/envelope/approve`,
      {
        expected_version: input.expected_version,
        allowed_providers: input.allowed_providers,
        budget_limits: input.budget_limits,
        acquire_hits_allowed: input.acquire_hits_allowed,
        max_acquire_volume: input.max_acquire_volume,
        disclosure: input.disclosure,
        reason: input.reason,
        correlation_id: input.correlation_id,
      },
      idempotencyKey,
    );
  },
  extendEnvelope(input, idempotencyKey) {
    return postJson(
      `${investigationUrl(input.investigation_id)}/envelope/extend`,
      {
        expected_version: input.expected_version,
        allowed_providers: input.allowed_providers,
        budget_limits: input.budget_limits,
        acquire_hits_allowed: input.acquire_hits_allowed,
        max_acquire_volume: input.max_acquire_volume,
        disclosure: input.disclosure,
        reason: input.reason,
        correlation_id: input.correlation_id,
      },
      idempotencyKey,
    );
  },
  revokeEnvelope(input, idempotencyKey) {
    return postJson(
      `${investigationUrl(input.investigation_id)}/envelope/revoke`,
      {
        expected_version: input.expected_version,
        reason: input.reason,
        correlation_id: input.correlation_id,
      },
      idempotencyKey,
    );
  },
  qualifySample(input, idempotencyKey) {
    return postJson(
      `${investigationUrl(input.investigation_id)}/samples/${encodeURIComponent(input.association_id)}/qualify`,
      {
        expected_version: input.expected_version,
        qualification: input.qualification,
        reason: input.reason,
        evidence_refs: input.evidence_refs,
        correlation_id: input.correlation_id,
      },
      idempotencyKey,
    );
  },
  requestAnalysis(input, idempotencyKey) {
    return postJson(
      `${investigationUrl(input.investigation_id)}/requests`,
      {
        text: input.text,
        target: input.target ?? null,
        expected_version: input.expected_version ?? null,
      },
      idempotencyKey,
    );
  },
};

export const investigationGovernanceQueryKeys = {
  envelope: (investigationId: string) =>
    ["investigation-envelope", investigationId] as const,
  samples: (investigationId: string) =>
    ["investigation-samples", investigationId] as const,
};

export function useInvestigationEnvelope(investigationId: string | null) {
  return useQuery({
    queryKey: investigationGovernanceQueryKeys.envelope(
      investigationId ?? "none",
    ),
    queryFn: () =>
      investigationGovernanceClient.getEnvelope(investigationId ?? ""),
    enabled: Boolean(investigationId),
  });
}

export function useInvestigationSamples(investigationId: string | null) {
  return useQuery({
    queryKey: investigationGovernanceQueryKeys.samples(
      investigationId ?? "none",
    ),
    queryFn: () =>
      investigationGovernanceClient.getSamples(investigationId ?? ""),
    enabled: Boolean(investigationId),
  });
}

function useGovernanceMutation<TInput, TResult>(
  mutationFn: (input: TInput, idempotencyKey: string) => Promise<TResult>,
  investigationIdFromInput: (input: TInput) => string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: TInput) => mutationFn(input, createIdempotencyKey()),
    onSuccess: (_result, input) => {
      const investigationId = investigationIdFromInput(input);
      void queryClient.invalidateQueries({
        queryKey: investigationQueryKeys.detail(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: ["subject-investigations"],
      });
      void queryClient.invalidateQueries({
        queryKey: investigationGovernanceQueryKeys.envelope(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: investigationGovernanceQueryKeys.samples(investigationId),
      });
    },
  });
}

const investigationId = (input: { investigation_id: string }): string =>
  input.investigation_id;

export function useApproveInvestigationEnvelope() {
  return useGovernanceMutation(
    (input: InvestigationEnvelopeDecisionInput, key) =>
      investigationGovernanceClient.approveEnvelope(input, key),
    investigationId,
  );
}

export function useExtendInvestigationEnvelope() {
  return useGovernanceMutation(
    (input: InvestigationEnvelopeDecisionInput, key) =>
      investigationGovernanceClient.extendEnvelope(input, key),
    investigationId,
  );
}

export function useRevokeInvestigationEnvelope() {
  return useGovernanceMutation(
    (input: InvestigationEnvelopeRevokeInput, key) =>
      investigationGovernanceClient.revokeEnvelope(input, key),
    investigationId,
  );
}

export function useQualifyInvestigationSample() {
  return useGovernanceMutation(
    (input: SampleQualificationDecisionInput, key) =>
      investigationGovernanceClient.qualifySample(input, key),
    investigationId,
  );
}

export function useRequestTargetedAnalysis() {
  return useGovernanceMutation(
    (input: InvestigationUserRequestInput, key) =>
      investigationGovernanceClient.requestAnalysis(input, key),
    investigationId,
  );
}

export type InvestigationGovernanceMutationResult =
  | InvestigationEnvelopeView
  | SampleQualificationResponse
  | InvestigationUserRequestResponse
  | InvestigationActionResponse;
