/**
 * Workspace contribution imports (`api/investigation_contributions.py`, 07).
 *
 * Every route is human-gated. The cockpit sends `origin: "human" | "host"`;
 * an `agent` origin requires the D7 session-bound path and is refused here.
 * A semantic refusal is a persisted terminal row carrying a typed
 * `validation_error_code`, not an HTTP error. Imports carry
 * `Idempotency-Key` and name an exact `(workspace_id, revision_id)` pair.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { tasksKeys } from "./tasks";
import {
  createIdempotencyKey,
  getJson,
  investigationPath,
  postJson,
  queryString,
} from "./client";
import type {
  CommonErrorCode,
  FindingAssessment,
  FindingKind,
  FindingNovelty,
  IsoTimestamp,
  JsonObject,
  Uuid,
} from "./types";

export type WorkContributionCategory =
  "work_contribution" | "observation" | "finding";
export type WorkContributionImportStatus = "imported" | "rejected" | "skipped";
export type WorkContributionObjectType =
  "sample" | "file" | "host" | "ip" | "domain" | "url";
export type WorkContributionMethod =
  | "hash"
  | "static_analysis"
  | "string_extraction"
  | "yara_scan"
  | "dynamic_behaviour"
  | "network_capture_analysis"
  | "dns_query"
  | "http_request"
  | "negative_check";
/** The UI never imports as `agent`: that origin needs the signed session path. */
export type WorkContributionOrigin = "human" | "host";
export type WorkContributionValidationCode =
  | "invalid_declaration"
  | "invalid_idempotency_key"
  | "invalid_file_relpath"
  | "object_method_not_allowed"
  | "observation_metadata_too_large"
  | "forbidden_file_type"
  | "contribution_too_large"
  | "empty_content"
  | "internal_workspace_path"
  | "workspace_revision_content_mismatch"
  | "observation_ref_not_found"
  | "observation_ref_investigation_mismatch"
  | "advisory_observation_not_evidence"
  | "finding_claim_ref_not_found"
  | "finding_invalid"
  | "sample_hash_invalid"
  | "task_not_in_investigation"
  | "lease_fence_required"
  | "lease_fence_stale"
  | "lease_expired";

export type ContributionErrorCode =
  | CommonErrorCode
  | "work_contribution_invalid"
  | "work_contribution_not_found"
  | "work_contribution_invalid_request"
  | "work_contribution_storage_unavailable"
  | "operator_session_required"
  | "workspace_local_unavailable"
  | "invalid_uuid"
  | "invalid_request"
  | WorkContributionValidationCode;

export interface ObservationDeclarationRequest {
  object_type: WorkContributionObjectType;
  method: WorkContributionMethod;
  object_id: string;
  tool: string;
  tool_version: string;
  parameters?: JsonObject;
  limitations?: string[];
  observed_at?: IsoTimestamp;
}

export interface FindingDeclarationRequest {
  kind: FindingKind;
  assessment: FindingAssessment;
  statement: string;
  observation_refs?: string[];
  claim_refs?: string[];
  novelty?: FindingNovelty;
  scope?: string;
  limitations?: string[];
  proposed_sample_sha256?: string;
}

interface ImportContributionEnvelope {
  workspace_id: string;
  workspace_revision_id: string;
  origin: WorkContributionOrigin;
  author_actor_id?: string;
  task_id?: Uuid;
}

/**
 * The route validates the category shape itself: a work contribution carries
 * no declaration, an observation import exactly one observation, a finding
 * import exactly one finding.
 */
export type ImportContributionRequest = ImportContributionEnvelope &
  (
    | {
        category: "work_contribution";
        observation?: never;
        finding?: never;
      }
    | {
        category: "observation";
        observation: ObservationDeclarationRequest;
        finding?: never;
      }
    | {
        category: "finding";
        finding: FindingDeclarationRequest;
        observation?: never;
      }
  );

export interface WorkContributionView {
  id: Uuid;
  investigation_id: Uuid;
  workspace_id: string;
  workspace_revision_id: string;
  file_relpath: string;
  file_sha256: string;
  category: WorkContributionCategory;
  schema_version: string;
  import_status: WorkContributionImportStatus;
  origin: "human" | "agent" | "host";
  origin_actor_id: string | null;
  task_id: Uuid | null;
  lease_fence: number | null;
  content_blob_id: Uuid | null;
  content_sha256: string | null;
  observation_id: Uuid | null;
  finding_id: Uuid | null;
  sample_association_id: Uuid | null;
  validation_error_code: WorkContributionValidationCode | null;
  citable_as_evidence: boolean;
  idempotency_key: string;
  observed_at: IsoTimestamp;
  created_at: IsoTimestamp;
}

export interface ContributionListParams {
  category?: WorkContributionCategory;
  import_status?: WorkContributionImportStatus;
  workspace_id?: string;
}

export const contributionsKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "contributions"] as const,
  list: (investigationId: string, params: ContributionListParams = {}) =>
    [
      ...contributionsKeys.all(investigationId),
      "list",
      params.category ?? "",
      params.import_status ?? "",
      params.workspace_id ?? "",
    ] as const,
  detail: (investigationId: string, contributionId: string) =>
    [...contributionsKeys.all(investigationId), contributionId] as const,
};

export function listContributions(
  investigationId: string,
  params: ContributionListParams = {},
  signal?: AbortSignal,
): Promise<WorkContributionView[]> {
  return getJson<WorkContributionView[]>(
    investigationPath(investigationId, "/contributions") +
      queryString({
        category: params.category,
        import_status: params.import_status,
        workspace_id: params.workspace_id,
      }),
    signal,
  );
}

export function importContribution(
  investigationId: string,
  input: ImportContributionRequest,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<WorkContributionView> {
  return postJson<WorkContributionView>(
    investigationPath(investigationId, "/contributions"),
    input,
    idempotencyKey,
    signal,
  );
}

export function getContribution(
  investigationId: string,
  contributionId: string,
  signal?: AbortSignal,
): Promise<WorkContributionView> {
  return getJson<WorkContributionView>(
    investigationPath(
      investigationId,
      `/contributions/${encodeURIComponent(contributionId)}`,
    ),
    signal,
  );
}

export function useContributions(
  investigationId: string,
  params: ContributionListParams = {},
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: contributionsKeys.list(investigationId, params),
    queryFn: ({ signal }) => listContributions(investigationId, params, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useContribution(
  investigationId: string,
  contributionId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: contributionsKeys.detail(investigationId, contributionId),
    queryFn: ({ signal }) =>
      getContribution(investigationId, contributionId, signal),
    enabled:
      options.enabled ?? (investigationId !== "" && contributionId !== ""),
  });
}

/**
 * Invalidation: an import appends a contribution row, may create an
 * observation/finding/sample association, and records a
 * `contribution_imported` task event when it is task-linked; the contribution
 * list, the task projections and the timeline are stale.
 */
export function useImportContribution(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: ImportContributionRequest) =>
      importContribution(investigationId, input, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: contributionsKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
    },
  });
}
