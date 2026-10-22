import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createIdempotencyKey,
  investigationRequest,
  investigationQueryKeys,
  type CitationViolationPayload,
  type InvestigationOpinionBlock,
  type OpinionBlockType,
  type OpinionQualification,
  type ProtocolDiagramKind,
} from "./investigation";

export interface OpinionProvenance {
  investigation_id: string;
  synthesis_artifact_id: string;
  synthesis_sha256: string;
  base_publication_artifact_id: string;
  base_publication_sha256: string;
}

export interface AnalystOpinionVersion {
  schema: "analyst-opinion.v1";
  id: string;
  version: number;
  investigation_id: string;
  synthesis_artifact_id: string;
  synthesis_sha256: string;
  base_publication_artifact_id: string;
  base_publication_sha256: string;
  blocks: InvestigationOpinionBlock[];
  protocol_figures?: OpinionProtocolFigure[];
  unresolved_points: string[];
  author_id: string;
  reviewer_id: string | null;
  status: "draft" | "approved" | "rejected";
  created_at: string;
  content_sha256: string;
  renderable_blocks: InvestigationOpinionBlock[];
  implant_materials: ImplantMaterialEdit[];
  implant_materials_section: ImplantMaterialsSection | null;
  provenance: OpinionProvenance;
}

export interface BridgeDraftReference {
  invocation_id: string;
  text_sha256: string;
}

export interface BridgeReviewReference {
  invocation_id: string;
  output_sha256: string;
  reviewed_text_sha256: string;
}

export interface ImplantMaterialsSection {
  type: "implant_materials";
  text: string;
  bridge_authored: boolean;
  bridge_draft_ref: BridgeDraftReference | null;
  bridge_review_ref: BridgeReviewReference | null;
}

export interface ImplantMaterialEdit {
  type:
    | "rc4_key"
    | "aes_key"
    | "passphrase"
    | "xor_scheme"
    | "attacker_github_c2_token"
    | "other";
  value: string;
  role: string;
  sample_location: string;
  sample_sha256: string;
  evidence_observation_id: string;
}

export interface OpinionProtocolFigure {
  kind: ProtocolDiagramKind;
  model_sha256: string;
  spec_sha256: string;
  asset_id: string;
  asset_sha256: string;
  preview_sha256: string;
  pin_sha256: string;
  compiler_name: string;
  compiler_version: string;
  compiler_policy_version: string;
  caption: string;
}

export interface AnalystOpinionResponse {
  draft: AnalystOpinionVersion | null;
  history: AnalystOpinionVersion[];
  citation_violations: CitationViolationPayload[];
  renderable_blocks: InvestigationOpinionBlock[];
  provenance: OpinionProvenance | null;
}

export interface OpinionCompositionSelection {
  finding_ids?: string[];
  claim_ids?: string[];
  block_types?: OpinionBlockType[];
}

export interface OpinionCompositionInput {
  selection?: OpinionCompositionSelection;
}

export interface OpinionBlockEdit {
  block_type: OpinionBlockType | "implant_materials";
  text: string;
  finding_refs: string[];
  evidence_refs: string[];
  qualification: OpinionQualification;
  certainty: number;
  bridge_authored?: boolean;
  bridge_draft_ref?: BridgeDraftReference | null;
  bridge_review_ref?: BridgeReviewReference | null;
}

export interface ReviseOpinionInput {
  expected_version: number;
  blocks: OpinionBlockEdit[];
  unresolved_points: string[];
  implant_materials?: ImplantMaterialEdit[];
  protocol_model_blob_id?: string;
  protocol_views?: ProtocolDiagramKind[];
}

export interface ReviewOpinionInput {
  version: number;
  expected_sha256: string;
  reason: string;
}

export interface PublishOpinionInput {
  version: number;
  expected_base_publication_sha256: string;
}

export interface OpinionDraftMutationResponse {
  draft: AnalystOpinionVersion;
}

export interface ApproveOpinionResponse {
  approved: AnalystOpinionVersion;
  reviewed_version: number;
  approved_version: number;
}

export interface RequestOpinionChangesResponse {
  opinion: AnalystOpinionVersion;
  changes_requested: true;
}

export interface PublishOpinionResponse {
  publication: {
    artifact_id: string;
    run_id: string;
    subject_id: string;
    version: number;
    sha256: string;
    previous_publication_artifact_id: string | null;
  };
  composition_repin_required: true;
  message: string;
}

function postJson<T>(url: string, payload: unknown): Promise<T> {
  return investigationRequest<T>(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": createIdempotencyKey(),
    },
    body: JSON.stringify(payload),
  });
}

export const analystOpinionQueryKeys = {
  detail: (investigationId: string) =>
    ["investigation-opinion", investigationId] as const,
};

export function getAnalystOpinion(
  investigationId: string,
): Promise<AnalystOpinionResponse> {
  return investigationRequest(
    `/api/investigations/${encodeURIComponent(investigationId)}/opinion`,
  );
}

export function analystOpinionPreviewPdfUrl(
  opinion: Pick<AnalystOpinionVersion, "id" | "version" | "content_sha256">,
  basePublicationSha256: string,
): string {
  const query = new URLSearchParams({
    version: String(opinion.version),
    opinion_sha256: opinion.content_sha256,
    base_publication_sha256: basePublicationSha256,
  });
  return `/api/opinions/${encodeURIComponent(opinion.id)}/preview.pdf?${query.toString()}`;
}

export function composeAnalystOpinion(
  investigationId: string,
  input: OpinionCompositionInput = {},
): Promise<OpinionDraftMutationResponse> {
  return postJson(
    `/api/investigations/${encodeURIComponent(investigationId)}/opinion/compose`,
    input,
  );
}

export function reviseAnalystOpinion(
  opinionId: string,
  input: ReviseOpinionInput,
): Promise<OpinionDraftMutationResponse> {
  return postJson(
    `/api/opinions/${encodeURIComponent(opinionId)}/revise`,
    input,
  );
}

export function approveAnalystOpinion(
  opinionId: string,
  input: ReviewOpinionInput,
): Promise<ApproveOpinionResponse> {
  return postJson(
    `/api/opinions/${encodeURIComponent(opinionId)}/approve`,
    input,
  );
}

export function requestAnalystOpinionChanges(
  opinionId: string,
  input: ReviewOpinionInput,
): Promise<RequestOpinionChangesResponse> {
  return postJson(
    `/api/opinions/${encodeURIComponent(opinionId)}/request-changes`,
    input,
  );
}

export function publishAnalystOpinion(
  opinionId: string,
  input: PublishOpinionInput,
): Promise<PublishOpinionResponse> {
  return postJson(
    `/api/opinions/${encodeURIComponent(opinionId)}/publish`,
    input,
  );
}

export function useAnalystOpinion(investigationId: string | null) {
  return useQuery({
    queryKey: analystOpinionQueryKeys.detail(investigationId ?? "none"),
    queryFn: () => getAnalystOpinion(investigationId ?? ""),
    enabled: Boolean(investigationId),
  });
}

function useOpinionMutation<TInput, TOutput>(
  mutationFn: (input: TInput) => Promise<TOutput>,
  investigationId: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: analystOpinionQueryKeys.detail(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: investigationQueryKeys.detail(investigationId),
      });
    },
  });
}

export function useComposeAnalystOpinion(investigationId: string) {
  return useOpinionMutation(
    (input: OpinionCompositionInput) =>
      composeAnalystOpinion(investigationId, input),
    investigationId,
  );
}

export function useReviseAnalystOpinion(investigationId: string) {
  return useOpinionMutation(
    (input: { opinionId: string } & ReviseOpinionInput) =>
      reviseAnalystOpinion(input.opinionId, {
        expected_version: input.expected_version,
        blocks: input.blocks,
        unresolved_points: input.unresolved_points,
        protocol_model_blob_id: input.protocol_model_blob_id,
        protocol_views: input.protocol_views,
      }),
    investigationId,
  );
}

export function useApproveAnalystOpinion(investigationId: string) {
  return useOpinionMutation(
    (input: { opinionId: string } & ReviewOpinionInput) =>
      approveAnalystOpinion(input.opinionId, {
        version: input.version,
        expected_sha256: input.expected_sha256,
        reason: input.reason,
      }),
    investigationId,
  );
}

export function useRequestAnalystOpinionChanges(investigationId: string) {
  return useOpinionMutation(
    (input: { opinionId: string } & ReviewOpinionInput) =>
      requestAnalystOpinionChanges(input.opinionId, {
        version: input.version,
        expected_sha256: input.expected_sha256,
        reason: input.reason,
      }),
    investigationId,
  );
}

export function usePublishAnalystOpinion(investigationId: string) {
  return useOpinionMutation(
    (input: { opinionId: string } & PublishOpinionInput) =>
      publishAnalystOpinion(input.opinionId, {
        version: input.version,
        expected_base_publication_sha256:
          input.expected_base_publication_sha256,
      }),
    investigationId,
  );
}
