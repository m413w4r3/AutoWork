/**
 * Bridge-authored Note de l’analyste proposals (`api/opinion_note_proposals.py`,
 * 08B).
 *
 * Drafting reuses the canonical `advisor.consult` path with a working-state
 * CAS. Acceptance is the only canonical write: it recopies the stored Bridge
 * text and records the human as accepter, never as author. No request body
 * carries note prose; the acceptance pins `expected_status_version` and the
 * optional base opinion version/hash pair.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { plansKeys } from "./plans";
import { tasksKeys } from "./tasks";
import {
  createIdempotencyKey,
  getJson,
  investigationPath,
  postJson,
  queryString,
} from "./client";
import type { CommonErrorCode, IsoTimestamp, Uuid } from "./types";

export type NoteProposalStatus =
  "proposed" | "accepted" | "rejected" | "stale" | "cancelled";
export type NoteTargetBlockType = "lead" | "section";
export type NoteProposalConsultationType = "draft_text" | "review_text";
export type AnalystObservationState = "observed" | "inferred" | "unresolved";
export type AnalystObservationRelation =
  "confirms" | "refutes" | "qualifies" | "unresolved";

export type NoteArticleElementKind = "table" | "chart" | "figure" | "diagram";
export type ProtocolFigureKind =
  "sequence" | "state_machine" | "message_format";

/** One M23 element already rendered in the pinned article, referenced by value. */
export interface ArticleElementNoteBlockV1 {
  block_type: "article_element";
  kind: NoteArticleElementKind;
  element_key: string;
  element_sha256: string;
  anchor: string;
}

/** An exact byte range of one canonical observation blob; never supplied text. */
export interface CanonicalCodeNoteBlockV1 {
  block_type: "canonical_code";
  observation_id: Uuid;
  blob_sha256: string;
  offset: number;
  length: number;
  excerpt_sha256: string;
  language: string;
}

/** Rows are derived from the cited observations; the title is deterministic. */
export interface IocTableNoteBlockV1 {
  block_type: "ioc_table";
  observation_ids: Uuid[];
  title: string;
}

/** A protocol view already carried by the opinion (annex). */
export interface ProtocolFigureNoteBlockV1 {
  block_type: "protocol_figure";
  kind: ProtocolFigureKind;
  asset_sha256: string;
}

/** Closed union of typed content a Bridge-drafted note may reference. */
export type NoteContentBlockV1 =
  | ArticleElementNoteBlockV1
  | CanonicalCodeNoteBlockV1
  | IocTableNoteBlockV1
  | ProtocolFigureNoteBlockV1;

export interface ResolvedArticleElementNoteBlockV1 extends ArticleElementNoteBlockV1 {
  title: string | null;
  caption: string | null;
}

export interface ResolvedCanonicalCodeNoteBlockV1 extends CanonicalCodeNoteBlockV1 {
  excerpt: string;
  observation_source: string;
  observation_object_type: string;
  observation_object_id: string;
}

export interface ResolvedIocTableRowV1 {
  observation_id: Uuid;
  object_type: string;
  object_id: string;
  source: string;
  observed_at: string;
  blob_sha256: string;
}

export interface ResolvedIocTableNoteBlockV1 extends IocTableNoteBlockV1 {
  rows: ResolvedIocTableRowV1[];
}

export interface ResolvedProtocolFigureNoteBlockV1 extends ProtocolFigureNoteBlockV1 {
  caption: string;
}

export type ResolvedNoteContentBlockV1 =
  | ResolvedArticleElementNoteBlockV1
  | ResolvedCanonicalCodeNoteBlockV1
  | ResolvedIocTableNoteBlockV1
  | ResolvedProtocolFigureNoteBlockV1;

/** `PROSE_BLOCK_TYPES`: the two projection blocks are refused here. */
export type ProseOpinionBlockType =
  | "key_findings"
  | "additional_samples"
  | "infection_chain_and_capabilities"
  | "network_and_infrastructure"
  | "claim_verification"
  | "proposed_detections"
  | "uncertainties_and_next_leads";

export type NoteProposalErrorCode =
  | CommonErrorCode
  | "note_proposal_not_found"
  | "invalid_note_anchor"
  | "invalid_observation_anchor"
  | "note_anchor_text_changed"
  | "note_block_ref_stale"
  | "note_code_ref_stale"
  | "note_chart_without_evidence"
  | "note_figure_source_missing"
  | "note_ioc_ref_unresolved"
  | "invalid_note_target"
  | "invalid_note_citation"
  | "advisory_observation_not_evidence"
  | "stale_note_proposal"
  | "bridge_provenance_invalid"
  | "bridge_response_pending"
  | "bridge_response_unusable"
  | "invalid_note_proposal_status"
  | "opinion_not_found"
  | "stale_version"
  | "version_hash_mismatch"
  | "citations_invalid"
  | "stale_base"
  | "invalid_opinion_state"
  | "bridge_authorship_required"
  | "bridge_prose_forbidden"
  | "bridge_modified_prose_forbidden"
  | "stale_working_state"
  | "bridge_checkpoint_pending"
  | "external_disclosure_denied"
  | "operator_payload_invalid_or_sensitive"
  | "operator_mode_required"
  | "requester_actor_id_required"
  | "invocation_attribution_invalid"
  | "idempotency_key_invalid"
  | "idempotency_scope_invalid"
  | "investigation_not_started";

export interface NoteProposalViewV1 {
  id: Uuid;
  investigation_id: Uuid;
  opinion_id: Uuid;
  task_id: Uuid | null;
  anchor: string;
  target_block_type: NoteTargetBlockType;
  bridge_block_type: ProseOpinionBlockType;
  consultation_type: NoteProposalConsultationType;
  base_note_id: Uuid | null;
  note_state: AnalystObservationState;
  note_relation: AnalystObservationRelation;
  cited_finding_refs: Uuid[];
  cited_observation_refs: Uuid[];
  proposed_text: string | null;
  proposed_text_sha256: string | null;
  bridge_invocation_id: string | null;
  bridge_output_sha256: string | null;
  bridge_response_observation_id: Uuid | null;
  tool_invocation_id: Uuid | null;
  base_opinion_version: number | null;
  base_opinion_sha256: string | null;
  /** Exact publication base and paragraph text pinned by the request. */
  base_publication_artifact_id: Uuid | null;
  base_publication_version: number | null;
  base_publication_sha256: string | null;
  anchor_before_sha256: string | null;
  content_blocks: NoteContentBlockV1[];
  status: NoteProposalStatus;
  status_version: number;
  accepted_opinion_version: number | null;
  analyst_decision_id: Uuid | null;
  idempotency_key: string;
  materialised: boolean;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
}

export interface NoteProposalDiffViewV1 {
  proposal: NoteProposalViewV1;
  current_opinion_version: number | null;
  current_opinion_sha256: string | null;
  current_note_id: Uuid | null;
  current_note_text: string | null;
  proposed_text: string | null;
  text_changed: boolean;
  anchor: string;
  target_block_type: NoteTargetBlockType;
  anchor_excerpt: string | null;
  cited_finding_refs: Uuid[];
  cited_observation_refs: Uuid[];
  content_blocks: NoteContentBlockV1[];
  resolved_content_blocks: ResolvedNoteContentBlockV1[];
  content_blocks_valid: boolean;
  content_block_error: string | null;
  limits: string[];
  stale: boolean;
  accept_ready: boolean;
}

interface NoteProposalRequestBase {
  anchor: string;
  target_block_type: NoteTargetBlockType;
  bridge_block_type: ProseOpinionBlockType;
  consultation_type?: NoteProposalConsultationType;
  note_state?: AnalystObservationState;
  note_relation?: AnalystObservationRelation;
  finding_refs?: Uuid[];
  observation_refs?: Uuid[];
  content_blocks?: NoteContentBlockV1[];
  task_id?: Uuid;
  question?: string;
  expected_working_state_version: number;
  expected_working_state_sha256?: string;
}

/** The validator requires the base opinion version and hash as a pair. */
type BaseOpinionPin =
  | { base_opinion_version: number; base_opinion_sha256: string }
  | { base_opinion_version?: never; base_opinion_sha256?: never };

export type NoteProposalRequestV1 = NoteProposalRequestBase & BaseOpinionPin;

export interface NoteProposalRequestResponseV1 {
  proposal: NoteProposalViewV1;
  invocation_id: Uuid;
  invocation_status: string;
  idempotent_replay: boolean;
  materialised: boolean;
}

export interface NoteProposalIngestV1 {
  expected_status_version: number;
}

export interface NoteProposalTransitionV1 {
  expected_status_version: number;
  reason?: string;
}

export interface NoteProposalAcceptV1 {
  expected_status_version: number;
  expected_base_version?: number;
  expected_base_sha256?: string;
  reason?: string;
}

export interface NoteAcceptanceViewV1 {
  proposal: NoteProposalViewV1;
  opinion_id: Uuid;
  opinion_version: number;
  opinion_sha256: string;
  decision_id: Uuid;
  note_id: Uuid;
  idempotent_replay: boolean;
}

export const opinionProposalsKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "note-proposals"] as const,
  list: (investigationId: string, status?: NoteProposalStatus) =>
    [
      ...opinionProposalsKeys.all(investigationId),
      "list",
      status ?? "",
    ] as const,
  detail: (investigationId: string, proposalId: string) =>
    [...opinionProposalsKeys.all(investigationId), proposalId] as const,
};

export function listNoteProposals(
  investigationId: string,
  status?: NoteProposalStatus,
  signal?: AbortSignal,
): Promise<NoteProposalViewV1[]> {
  return getJson<NoteProposalViewV1[]>(
    investigationPath(investigationId, "/note-proposals") +
      queryString({ status }),
    signal,
  );
}

export function getNoteProposal(
  investigationId: string,
  proposalId: string,
  signal?: AbortSignal,
): Promise<NoteProposalDiffViewV1> {
  return getJson<NoteProposalDiffViewV1>(
    investigationPath(
      investigationId,
      `/note-proposals/${encodeURIComponent(proposalId)}`,
    ),
    signal,
  );
}

export function requestNoteProposal(
  investigationId: string,
  input: NoteProposalRequestV1,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<NoteProposalRequestResponseV1> {
  return postJson<NoteProposalRequestResponseV1>(
    investigationPath(investigationId, "/note-proposals"),
    input,
    idempotencyKey,
    signal,
  );
}

/** Internal ingestion is CAS-only (no Idempotency-Key in the route). */
export function ingestNoteProposalResponse(
  investigationId: string,
  proposalId: string,
  input: NoteProposalIngestV1,
  signal?: AbortSignal,
): Promise<NoteProposalViewV1> {
  return postJson<NoteProposalViewV1>(
    investigationPath(
      investigationId,
      `/note-proposals/${encodeURIComponent(proposalId)}/ingest`,
    ),
    input,
    undefined,
    signal,
  );
}

export function acceptNoteProposal(
  investigationId: string,
  proposalId: string,
  input: NoteProposalAcceptV1,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<NoteAcceptanceViewV1> {
  return postJson<NoteAcceptanceViewV1>(
    investigationPath(
      investigationId,
      `/note-proposals/${encodeURIComponent(proposalId)}/accept`,
    ),
    input,
    idempotencyKey,
    signal,
  );
}

export function rejectNoteProposal(
  investigationId: string,
  proposalId: string,
  input: NoteProposalTransitionV1,
  signal?: AbortSignal,
): Promise<NoteProposalViewV1> {
  return postJson<NoteProposalViewV1>(
    investigationPath(
      investigationId,
      `/note-proposals/${encodeURIComponent(proposalId)}/reject`,
    ),
    input,
    undefined,
    signal,
  );
}

export function cancelNoteProposal(
  investigationId: string,
  proposalId: string,
  input: NoteProposalTransitionV1,
  signal?: AbortSignal,
): Promise<NoteProposalViewV1> {
  return postJson<NoteProposalViewV1>(
    investigationPath(
      investigationId,
      `/note-proposals/${encodeURIComponent(proposalId)}/cancel`,
    ),
    input,
    undefined,
    signal,
  );
}

export function useNoteProposals(
  investigationId: string,
  status?: NoteProposalStatus,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: opinionProposalsKeys.list(investigationId, status),
    queryFn: ({ signal }) => listNoteProposals(investigationId, status, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useNoteProposal(
  investigationId: string,
  proposalId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: opinionProposalsKeys.detail(investigationId, proposalId),
    queryFn: ({ signal }) =>
      getNoteProposal(investigationId, proposalId, signal),
    enabled: options.enabled ?? (investigationId !== "" && proposalId !== ""),
  });
}

/**
 * Invalidation: a drafting request appends an invocation and a consultation
 * ledger row, then the proposal itself; the proposal reads, the consultation
 * ledger and the timeline are stale.
 */
export function useRequestNoteProposal(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: NoteProposalRequestV1) =>
      requestNoteProposal(investigationId, input, createIdempotencyKey()),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: opinionProposalsKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: plansKeys.consultations(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
    },
  });
}

/** Invalidation: ingestion materialises the stored Bridge text on the proposal. */
export function useIngestNoteProposalResponse(
  investigationId: string,
  proposalId: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: NoteProposalIngestV1) =>
      ingestNoteProposalResponse(investigationId, proposalId, input),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: opinionProposalsKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: plansKeys.consultations(investigationId),
      });
    },
  });
}

/**
 * Invalidation: acceptance appends a canonical opinion version, a human
 * decision and a timeline item. The opinion feature that owns the opinion
 * query keys (outside this API folder) must invalidate them too.
 */
export function useAcceptNoteProposal(
  investigationId: string,
  proposalId: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: NoteProposalAcceptV1) =>
      acceptNoteProposal(
        investigationId,
        proposalId,
        input,
        createIdempotencyKey(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: opinionProposalsKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
    },
  });
}

/** Invalidation: a rejection/cancellation is a status transition only. */
export function useRejectNoteProposal(
  investigationId: string,
  proposalId: string,
) {
  return useProposalTransition(investigationId, proposalId, rejectNoteProposal);
}

/** Invalidation: a rejection/cancellation is a status transition only. */
export function useCancelNoteProposal(
  investigationId: string,
  proposalId: string,
) {
  return useProposalTransition(investigationId, proposalId, cancelNoteProposal);
}

function useProposalTransition(
  investigationId: string,
  proposalId: string,
  mutationFn: (
    investigationId: string,
    proposalId: string,
    input: NoteProposalTransitionV1,
  ) => Promise<NoteProposalViewV1>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: NoteProposalTransitionV1) =>
      mutationFn(investigationId, proposalId, input),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: opinionProposalsKeys.all(investigationId),
      });
    },
  });
}
