/**
 * Human-only VT bootstrap and initial G2 envelope contract
 * (`api/investigation_bootstrap.py`, `api/investigation_bootstrap_envelope.py`).
 *
 * Every route is gated by the verified-human identity server-side. The
 * bootstrap batch and the envelope approval carry `Idempotency-Key`;
 * approval also pins the exact `proposal_sha256` and an optional CAS
 * `expected_version` of the persisted envelope.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createIdempotencyKey,
  getJson,
  investigationPath,
  postEmpty,
  postJson,
} from "./client";
import type {
  BudgetDimension,
  CommonErrorCode,
  IsoTimestamp,
  JsonObject,
  SampleOrigin,
  SampleQualification,
  SampleState,
  TLPLevel,
  ToolInvocationStatus,
  Uuid,
} from "./types";

export type BootstrapIocKind = "sample" | "ipv4" | "domain" | "url";
export type BootstrapIocOrigin = "seed" | "discovered";
export type BootstrapPlanState =
  "empty" | "awaiting_g2" | "ready" | "blocked" | "unavailable";
export type BootstrapRouteStatus = "configured" | "unverified" | "unavailable";
export type BootstrapCoverageKind =
  | "capability_absent"
  | "capability_shared"
  | "route_unavailable"
  | "route_unverified"
  | "deferred"
  | "limit";
export type BootstrapReason =
  | "pending"
  | "available"
  | "downloaded"
  | "reused"
  | "empty"
  | "not_found"
  | "provider_denied"
  | "route_unavailable"
  | "quota"
  | "failed"
  | "partial"
  | "needs_review"
  | "policy_denied"
  | "capability_absent"
  | "provider_not_allowed"
  | "acquisition_not_allowed"
  | "disclosure_bounds"
  | "budget_exceeded"
  | "awaiting_g2"
  | "evidence_unavailable"
  | "not_attempted"
  | "stopped"
  | "invocation_failed";

export type BootstrapErrorCode =
  | CommonErrorCode
  | "bootstrap_error"
  | "bootstrap_snapshot_not_found"
  | "bootstrap_snapshot_unavailable"
  | "bootstrap_state_unavailable"
  | "bootstrap_export_refused"
  | "bootstrap_envelope_error"
  | "bootstrap_proposal_stale"
  | "publication_kind_not_article"
  | "workspace_synthesis_pin_mismatch"
  | "envelope_conflict"
  | "invalid_envelope";

export interface BootstrapHashAliasV1 {
  hash_type: "md5" | "sha1" | "sha256";
  value: string;
}

export interface BootstrapIocSourceV1 {
  source_document_ids: Uuid[];
  source_refs: string[];
  extraction_artifact_id: Uuid | null;
  extraction_artifact_version: number | null;
  claim_ids: string[];
}

export interface BootstrapSampleAssociationV1 {
  association_id: Uuid;
  sample_sha256: string;
  origin: SampleOrigin;
  qualification: SampleQualification;
  version: number;
}

export interface BootstrapQuarantinedSampleV1 {
  sample_id: Uuid;
  sha256: string;
  state: SampleState;
  association_id: Uuid | null;
  qualification: SampleQualification | null;
}

export interface BootstrapIocV1 {
  ioc_id: string;
  kind: BootstrapIocKind;
  origin: BootstrapIocOrigin;
  value: string;
  hash_type: "md5" | "sha1" | "sha256" | null;
  hashes: BootstrapHashAliasV1[];
  sources: BootstrapIocSourceV1[];
  claims: string[];
  associations: BootstrapSampleAssociationV1[];
  quarantined: BootstrapQuarantinedSampleV1[];
  limitations: string[];
}

export interface BootstrapInventoryLimitsV1 {
  max_iocs: number;
  max_sources_per_ioc: number;
  max_claims_per_ioc: number;
  iocs_considered: number;
  truncated: boolean;
}

export interface IOCInventoryV1 {
  schema_version: "vt-bootstrap-inventory.v1";
  generated_at: IsoTimestamp;
  investigation_id: Uuid;
  subject_id: Uuid;
  production_run_id: Uuid;
  synthesis_artifact_id: Uuid;
  pinned_synthesis_sha256: string;
  workspace_binding_id: string | null;
  workspace_synthesis_sha256: string | null;
  seeds: BootstrapIocV1[];
  discovered: BootstrapIocV1[];
  limits: BootstrapInventoryLimitsV1;
  limitations: string[];
}

export interface BootstrapPlannedOperationV1 {
  operation_key: string;
  ioc_id: string;
  tool_name: string;
  schema_version: "1";
  arguments: JsonObject;
  status: ToolInvocationStatus;
  reason: BootstrapReason | null;
  reserved_vt_units: number;
  reserved_downloads: number;
  requires_batch_evidence: boolean;
  route_status: BootstrapRouteStatus;
  limitations: string[];
}

export interface BootstrapCoverageEntryV1 {
  kind: BootstrapCoverageKind;
  target: string;
  detail: string;
  blocking: boolean;
}

export interface BootstrapBudgetViewV1 {
  vt_units_remaining: number | null;
  downloads_remaining: number | null;
}

export interface BootstrapPlanLimitsV1 {
  max_operations: number;
  operations_planned: number;
  operations_truncated: boolean;
  max_relationships_per_object: number;
}

export interface BootstrapPlanV1 {
  schema_version: "vt-bootstrap-plan.v1";
  generated_at: IsoTimestamp;
  investigation_id: Uuid;
  state: BootstrapPlanState;
  envelope_version: number | null;
  envelope_approval_decision_id: Uuid | null;
  disclosure_tlp: TLPLevel | null;
  budget: BootstrapBudgetViewV1;
  operations: BootstrapPlannedOperationV1[];
  coverage: BootstrapCoverageEntryV1[];
  limits: BootstrapPlanLimitsV1;
  limitations: string[];
}

export interface BootstrapSnapshotOperationV1 {
  operation_key: string;
  ioc_id: string;
  tool_name: string;
  status: ToolInvocationStatus;
  reason: BootstrapReason | null;
  invocation_id: Uuid | null;
  idempotent_replay: boolean;
  observation_refs: Uuid[];
  budget_consumed: Record<string, number>;
  pages: number | null;
  observed_count: number | null;
  pagination_complete: boolean | null;
  next_cursor: string | null;
  provider_routes: string[];
  detail_code: string | null;
  error_code: string | null;
  limitations: string[];
}

export interface BootstrapDownloadOutcomeV1 {
  status: "downloaded" | "reused" | "not_attempted" | "failed";
  association_id: Uuid | null;
  sample_id: Uuid | null;
  sample_sha256: string | null;
  qualification: SampleQualification | null;
  hash_verified_quarantine: boolean;
  reason: string | null;
}

export interface BootstrapSnapshotIocV1 {
  ioc_id: string;
  kind: BootstrapIocKind;
  value: string;
  hash_type: "md5" | "sha1" | "sha256" | null;
  origin: BootstrapIocOrigin;
  status: ToolInvocationStatus;
  reason: BootstrapReason | null;
  operations: BootstrapSnapshotOperationV1[];
  download: BootstrapDownloadOutcomeV1 | null;
  association_ids: Uuid[];
  quarantined_sample_ids: Uuid[];
}

export interface BootstrapSnapshotLimitsV1 {
  max_iocs: number;
  iocs_included: number;
  inventory_truncated: boolean;
  max_operations: number;
  operations_recorded: number;
  operations_truncated: boolean;
  stopped_reason: BootstrapReason | null;
}

export interface BootstrapSnapshotV1 {
  schema_version: "vt-bootstrap-snapshot.v1";
  snapshot_id: Uuid;
  investigation_id: Uuid;
  pinned_synthesis_sha256: string;
  workspace_id: string | null;
  generated_at: IsoTimestamp;
  plan_state: BootstrapPlanState;
  envelope_version: number | null;
  inventory_sha256: string;
  plan_sha256: string;
  iocs: BootstrapSnapshotIocV1[];
  coverage: BootstrapCoverageEntryV1[];
  limits: BootstrapSnapshotLimitsV1;
  limitations: string[];
}

export interface BootstrapExportFileV1 {
  path: string;
  size_bytes: number;
  content: string;
}

export interface BootstrapSnapshotExportV1 {
  schema_version: "vt-bootstrap-export.v1";
  snapshot_id: Uuid;
  files: BootstrapExportFileV1[];
}

export interface BootstrapToolOperationCountV1 {
  tool_name:
    "vt.file_report" | "vt.object_report" | "vt.relationships" | "vt.behaviour";
  planned: number;
  covered: number;
}

export interface BootstrapVtReadsProposalV1 {
  allowed: boolean;
  provider: "vt";
  operations_planned: number;
  operations_covered: number;
  units_planned: number;
  units_proposed: number;
  iocs_planned: number;
  iocs_covered: number;
  per_tool: BootstrapToolOperationCountV1[];
  summary: string;
  limitations: string[];
}

export interface BootstrapSampleAcquisitionProposalV1 {
  allowed: boolean;
  acquire_hits_allowed: boolean;
  sample_count_planned: number;
  sample_count_covered: number;
  downloads_limit: number;
  per_sample_max_bytes: number;
  total_max_bytes: number;
  route_status: BootstrapRouteStatus;
  summary: string;
  limitations: string[];
}

export interface BootstrapBudgetLimitV1 {
  dimension: BudgetDimension;
  limit: number;
  source: "inventory" | "settings_cap" | "settings_default";
}

export interface BootstrapSettingsCapsV1 {
  vt_units: number;
  downloads: number;
  per_sample_bytes: number;
  tool_seconds: number;
  context_bytes: number;
  context_tokens: number;
}

export interface BootstrapCostsProposalV1 {
  limits: BootstrapBudgetLimitV1[];
  caps: BootstrapSettingsCapsV1;
  summary: string;
}

export interface BootstrapExternalModelProposalV1 {
  allowed: boolean;
  providers: string[];
  policy_source: "article_policy";
  summary: string;
  limitations: string[];
}

export interface BootstrapDisclosureProposalV1 {
  tlp: TLPLevel | null;
  do_not_submit: boolean;
  external_llm_allowed: boolean;
  scope: "article_cited_iocs";
  scope_statement: string;
  summary: string;
  limitations: string[];
}

export interface BootstrapInventoryCountsV1 {
  seeds: number;
  discovered: number;
  samples: number;
  ipv4: number;
  domains: number;
  urls: number;
  truncated: boolean;
}

export interface BootstrapEnvelopeProposalV1 {
  schema_version: "vt-bootstrap-envelope-proposal.v1";
  generated_at: IsoTimestamp;
  investigation_id: Uuid;
  proposal_sha256: string;
  approved: false;
  already_approved: boolean;
  envelope_version: number | null;
  plan_state: BootstrapPlanState;
  allowed_providers: string[];
  acquire_hits_allowed: boolean;
  inventory: BootstrapInventoryCountsV1;
  planned_operations: number;
  covered_operations: number;
  covered_ioc_ids: string[];
  vt_reads: BootstrapVtReadsProposalV1;
  sample_acquisition: BootstrapSampleAcquisitionProposalV1;
  costs: BootstrapCostsProposalV1;
  external_model: BootstrapExternalModelProposalV1;
  disclosure: BootstrapDisclosureProposalV1;
  summary: string[];
  limitations: string[];
}

export interface BootstrapEnvelopeApprovalV1 {
  schema_version: "vt-bootstrap-envelope-approval.v1";
  approved: true;
  investigation_id: Uuid;
  proposal: BootstrapEnvelopeProposalV1;
  envelope_version: number;
  approval_decision_id: Uuid;
  approval_actor_id: string;
  approved_at: IsoTimestamp;
  allowed_providers: string[];
  acquire_hits_allowed: boolean;
  budget_limits: Record<string, number>;
  disclosure_tlp: TLPLevel | null;
  disclosure_do_not_submit: boolean;
  disclosure_external_llm_allowed: boolean;
}

export interface BootstrapEnvelopeApprovalInput {
  reason: string;
  correlation_id: string;
  proposal_sha256: string;
  expected_version?: number;
}

export const bootstrapKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "bootstrap"] as const,
  inventory: (investigationId: string) =>
    [...bootstrapKeys.all(investigationId), "inventory"] as const,
  plan: (investigationId: string) =>
    [...bootstrapKeys.all(investigationId), "plan"] as const,
  snapshot: (investigationId: string) =>
    [...bootstrapKeys.all(investigationId), "snapshot"] as const,
  snapshotExport: (investigationId: string) =>
    [...bootstrapKeys.all(investigationId), "snapshot-export"] as const,
  envelopeProposal: (investigationId: string) =>
    [...bootstrapKeys.all(investigationId), "envelope-proposal"] as const,
};

export function getBootstrapInventory(
  investigationId: string,
  signal?: AbortSignal,
): Promise<IOCInventoryV1> {
  return getJson<IOCInventoryV1>(
    investigationPath(investigationId, "/bootstrap/inventory"),
    signal,
  );
}

export function getBootstrapPlan(
  investigationId: string,
  signal?: AbortSignal,
): Promise<BootstrapPlanV1> {
  return getJson<BootstrapPlanV1>(
    investigationPath(investigationId, "/bootstrap/plan"),
    signal,
  );
}

export function getBootstrapSnapshot(
  investigationId: string,
  signal?: AbortSignal,
): Promise<BootstrapSnapshotV1> {
  return getJson<BootstrapSnapshotV1>(
    investigationPath(investigationId, "/bootstrap/snapshot"),
    signal,
  );
}

export function getBootstrapSnapshotExport(
  investigationId: string,
  signal?: AbortSignal,
): Promise<BootstrapSnapshotExportV1> {
  return getJson<BootstrapSnapshotExportV1>(
    investigationPath(investigationId, "/bootstrap/snapshot/export"),
    signal,
  );
}

/** Runs the bounded VT read/download batch; `Idempotency-Key` is required. */
export function runBootstrap(
  investigationId: string,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<BootstrapSnapshotV1> {
  return postEmpty<BootstrapSnapshotV1>(
    investigationPath(investigationId, "/bootstrap/run"),
    idempotencyKey,
    signal,
  );
}

export function getBootstrapEnvelopeProposal(
  investigationId: string,
  signal?: AbortSignal,
): Promise<BootstrapEnvelopeProposalV1> {
  return getJson<BootstrapEnvelopeProposalV1>(
    investigationPath(investigationId, "/bootstrap/envelope-proposal"),
    signal,
  );
}

export function approveBootstrapEnvelope(
  investigationId: string,
  input: BootstrapEnvelopeApprovalInput,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<BootstrapEnvelopeApprovalV1> {
  return postJson<BootstrapEnvelopeApprovalV1>(
    investigationPath(investigationId, "/bootstrap/envelope/approve"),
    input,
    idempotencyKey,
    signal,
  );
}

export function useBootstrapInventory(
  investigationId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: bootstrapKeys.inventory(investigationId),
    queryFn: ({ signal }) => getBootstrapInventory(investigationId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useBootstrapPlan(
  investigationId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: bootstrapKeys.plan(investigationId),
    queryFn: ({ signal }) => getBootstrapPlan(investigationId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useBootstrapSnapshot(
  investigationId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: bootstrapKeys.snapshot(investigationId),
    queryFn: ({ signal }) => getBootstrapSnapshot(investigationId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useBootstrapSnapshotExport(
  investigationId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: bootstrapKeys.snapshotExport(investigationId),
    queryFn: ({ signal }) =>
      getBootstrapSnapshotExport(investigationId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useBootstrapEnvelopeProposal(
  investigationId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: bootstrapKeys.envelopeProposal(investigationId),
    queryFn: ({ signal }) =>
      getBootstrapEnvelopeProposal(investigationId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

/**
 * Invalidation: the run appends operations, observations and a new snapshot,
 * so every bootstrap read of this investigation is stale; the plan and the
 * envelope proposal recompute their state from that snapshot as well.
 */
export function useRunBootstrap(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => runBootstrap(investigationId, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: bootstrapKeys.all(investigationId),
      });
    },
  });
}

/**
 * Invalidation: approval writes an envelope version and its G2 decision, so
 * the proposal, the plan state, the snapshot display and the bootstrap reads
 * must be refetched.
 */
export function useApproveBootstrapEnvelope(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: BootstrapEnvelopeApprovalInput) =>
      approveBootstrapEnvelope(investigationId, input, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: bootstrapKeys.all(investigationId),
      });
    },
  });
}
