/**
 * Human note annotations and note actions
 * (`api/investigation_workspace_notes.py`, WS2).
 *
 * A note is a non-evidence work object. Reading returns the canonical revision
 * history and annotations, never host bytes. A Bridge review reuses
 * `advisor.consult` (G2/TLP/external-LLM gate); an agent continuation is a
 * persisted session instruction; a verification task is created `proposed`
 * through the 04B engine; an observation proposal writes nothing.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { agentSessionsKeys } from "./agentSessions";
import { plansKeys } from "./plans";
import { tasksKeys } from "./tasks";
import {
  createIdempotencyKey,
  getJson,
  investigationPath,
  postJson,
} from "./client";
import type {
  CommonErrorCode,
  EvidenceKind,
  InstructionState,
  IsoTimestamp,
  JsonObject,
  ResultVerificationStatus,
  TaskActorKind,
  TaskStatus,
  ToolInvocationStatus,
  Uuid,
  WorkspaceOrigin,
} from "./types";
import type {
  WorkContributionCategory,
  WorkContributionImportStatus,
  WorkContributionMethod as NoteContributionMethod,
  WorkContributionObjectType as NoteContributionObjectType,
} from "./contributions";

export type { NoteContributionMethod, NoteContributionObjectType };

export type NoteAnnotationKind =
  | "link_task"
  | "link_ioc"
  | "link_sample"
  | "mark_useful"
  | "unmark_useful"
  | "attribute_author"
  | "dismiss";
export type NoteIocType = "md5" | "sha1" | "sha256" | "ipv4" | "domain" | "url";

export type NoteErrorCode =
  | CommonErrorCode
  | "workspace_not_bound"
  | "note_not_found"
  | "invalid_note_path"
  | "invalid_note_annotation"
  | "task_not_in_investigation"
  | "ioc_not_in_investigation"
  | "sample_not_in_investigation"
  | "invalid_note_citation"
  | "advisory_observation_not_evidence"
  | "invalid_note_passage"
  | "invalid_observation_conversion"
  | "note_action_unavailable"
  | "invalid_note_request"
  | "agent_session_invalid"
  | "agent_session_not_found"
  | "agent_session_actor_not_allowed"
  | "active_session_exists"
  | "agent_session_state_conflict"
  | "instruction_conflict"
  | "resume_unavailable"
  | "new_handoff_required"
  | "agentic_operator_unavailable"
  | "agentic_operator_contract_invalid"
  | "agent_session_start_failed"
  | "agent_session_secret_delivery_unavailable"
  | "agent_session_secret_delivery_failed"
  | "note_refs_unavailable"
  | "note_ref_unresolved"
  | "note_ref_invalid"
  | "task_not_found"
  | "task_actor_not_allowed"
  | "task_transition_not_allowed"
  | "stale_task_version"
  | "lease_expired"
  | "stale_fence"
  | "task_limit_exceeded"
  | "task_dependency_invalid"
  | "task_key_conflict"
  | "task_command_invalid"
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

export interface NoteAnnotationRequestV1 {
  kind: NoteAnnotationKind;
  task_id?: Uuid;
  ioc_value?: string;
  ioc_type?: NoteIocType;
  sample_id?: Uuid;
  detail?: string;
}

export interface AskBridgeRequestV1 {
  consultation_type?: "interpretation" | "critique";
  question: string;
  note_excerpt?: string;
  passage_start?: number;
  passage_end?: number;
  evidence_refs?: Uuid[];
  finding_refs?: Uuid[];
  expected_working_state_version: number;
  expected_working_state_sha256?: string;
}

export interface NoteAskAgentRequestV1 {
  task_id: Uuid;
  session_id?: Uuid;
  question: string;
}

export interface NoteVerificationTaskRequestV1 {
  passage_start: number;
  passage_end: number;
  question: string;
  evidence_refs?: Uuid[];
  claim_refs?: string[];
  task_id?: Uuid;
}

export interface NoteObservationProposalRequestV1 {
  object_type: NoteContributionObjectType;
  method: NoteContributionMethod;
  tool: string;
  tool_version?: string;
  object_id?: string;
  parameters?: JsonObject;
  limitations?: string[];
  passage_start?: number;
  passage_end?: number;
  observed_at?: string;
}

export interface NoteAnnotationViewV1 {
  id: Uuid;
  investigation_id: Uuid;
  workspace_id: string;
  revision_id: string;
  file_relpath: string;
  kind: NoteAnnotationKind;
  actor_kind: "human";
  actor_id: string;
  target_task_id: Uuid | null;
  ioc_value: string | null;
  ioc_type: NoteIocType | null;
  target_sample_id: Uuid | null;
  detail: string | null;
  schema_version: string;
  idempotency_key: string;
  created_at: IsoTimestamp;
}

export interface NoteRevisionViewV1 {
  revision_id: string;
  path: string;
  sha256: string;
  size: number;
  observed_at: IsoTimestamp;
  origin: WorkspaceOrigin;
  supersedes_revision_id: string | null;
}

export interface NoteContributionViewV1 {
  id: Uuid;
  category: WorkContributionCategory;
  import_status: WorkContributionImportStatus;
  observation_id: Uuid | null;
  finding_id: Uuid | null;
}

export interface NoteInstructionUsageViewV1 {
  instruction_id: Uuid;
  session_id: Uuid;
  state: InstructionState;
  text_sha256: string;
}

export interface NoteHandoffUsageViewV1 {
  handoff_id: Uuid;
  task_id: Uuid;
  version: number;
}

export interface NoteConsultationUsageViewV1 {
  invocation_id: Uuid;
  operation_key: string;
  consultation_type: string;
  status: ToolInvocationStatus;
}

export interface NoteIocLinkViewV1 {
  ioc_type: NoteIocType;
  value: string;
}

export interface NoteViewResponseV1 {
  investigation_id: Uuid;
  workspace_id: string;
  path: string;
  latest: NoteRevisionViewV1;
  versions: NoteRevisionViewV1[];
  annotations: NoteAnnotationViewV1[];
  linked_task_ids: Uuid[];
  linked_iocs: NoteIocLinkViewV1[];
  linked_sample_ids: Uuid[];
  useful: boolean;
  dismissed: boolean;
  attributed_authors: string[];
  observation_imported: boolean;
  is_evidence: boolean;
  unverified_hypothesis: boolean;
  contributions: NoteContributionViewV1[];
  instructions: NoteInstructionUsageViewV1[];
  handoffs: NoteHandoffUsageViewV1[];
  consultations: NoteConsultationUsageViewV1[];
}

export interface NoteBridgeReviewResponseV1 {
  investigation_id: Uuid;
  note_revision_id: string;
  note_sha256: string;
  consultation_type: "interpretation" | "critique";
  invocation_id: Uuid;
  invocation_status: ToolInvocationStatus;
  pending: boolean;
  idempotent_replay: boolean;
  advisory_only: boolean;
  is_evidence: boolean;
}

export interface NoteAgentContinuationResponseV1 {
  investigation_id: Uuid;
  note_revision_id: string;
  note_sha256: string;
  note_path: string;
  task_id: Uuid;
  session_id: Uuid;
  instruction_id: Uuid;
  instruction_state: InstructionState;
  instruction_sha256: string;
}

export interface NoteVerificationTaskResponseV1 {
  task_id: Uuid;
  investigation_id: Uuid;
  task_key: string;
  title: string;
  status: TaskStatus;
  status_version: number;
  result_verification_status: ResultVerificationStatus;
  created_by_actor_kind: TaskActorKind;
  created_by_actor_id: string;
  replayed: boolean;
}

export interface NoteObservationProposalResponseV1 {
  investigation_id: Uuid;
  workspace_id: string;
  note_revision_id: string;
  note_sha256: string;
  note_path: string;
  passage: { start: number; end: number } | null;
  object_type: NoteContributionObjectType;
  method: NoteContributionMethod;
  object_id: string;
  tool: string;
  tool_version: string;
  parameters: JsonObject;
  limitations: string[];
  evidence_kind: EvidenceKind;
  import_category: WorkContributionCategory;
  import_service: string;
  requires_human_decision: boolean;
  canonical_writes: string[];
  is_evidence: boolean;
}

export const workspaceNotesKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "workspace-notes"] as const,
  view: (investigationId: string, notePath: string) =>
    [...workspaceNotesKeys.all(investigationId), "view", notePath] as const,
};

/** `{note_path:path}` must keep its slashes but escape each segment. */
export function encodeNotePath(notePath: string): string {
  return notePath
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
}

function noteActionPath(
  investigationId: string,
  revisionId: string,
  action: string,
): string {
  return investigationPath(
    investigationId,
    `/workspace/notes/${encodeURIComponent(revisionId)}/${action}`,
  );
}

export function getWorkspaceNoteView(
  investigationId: string,
  notePath: string,
  signal?: AbortSignal,
): Promise<NoteViewResponseV1> {
  return getJson<NoteViewResponseV1>(
    investigationPath(
      investigationId,
      `/workspace/notes/${encodeNotePath(notePath)}/view`,
    ),
    signal,
  );
}

export function annotateWorkspaceNote(
  investigationId: string,
  revisionId: string,
  input: NoteAnnotationRequestV1,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<NoteAnnotationViewV1> {
  return postJson<NoteAnnotationViewV1>(
    noteActionPath(investigationId, revisionId, "annotations"),
    input,
    idempotencyKey,
    signal,
  );
}

export function askBridgeToReviewNote(
  investigationId: string,
  revisionId: string,
  input: AskBridgeRequestV1,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<NoteBridgeReviewResponseV1> {
  return postJson<NoteBridgeReviewResponseV1>(
    noteActionPath(investigationId, revisionId, "ask-bridge"),
    input,
    idempotencyKey,
    signal,
  );
}

export function askAgentToContinueFromNote(
  investigationId: string,
  revisionId: string,
  input: NoteAskAgentRequestV1,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<NoteAgentContinuationResponseV1> {
  return postJson<NoteAgentContinuationResponseV1>(
    noteActionPath(investigationId, revisionId, "ask-agent"),
    input,
    idempotencyKey,
    signal,
  );
}

export function proposeNoteVerificationTask(
  investigationId: string,
  revisionId: string,
  input: NoteVerificationTaskRequestV1,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<NoteVerificationTaskResponseV1> {
  return postJson<NoteVerificationTaskResponseV1>(
    noteActionPath(investigationId, revisionId, "verification-task"),
    input,
    idempotencyKey,
    signal,
  );
}

/** A typed declaration only: the route writes nothing and takes no key. */
export function proposeNoteObservationConversion(
  investigationId: string,
  revisionId: string,
  input: NoteObservationProposalRequestV1,
  signal?: AbortSignal,
): Promise<NoteObservationProposalResponseV1> {
  return postJson<NoteObservationProposalResponseV1>(
    noteActionPath(investigationId, revisionId, "observation-proposal"),
    input,
    undefined,
    signal,
  );
}

export function useWorkspaceNoteView(
  investigationId: string,
  notePath: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: workspaceNotesKeys.view(investigationId, notePath),
    queryFn: ({ signal }) =>
      getWorkspaceNoteView(investigationId, notePath, signal),
    enabled: options.enabled ?? (investigationId !== "" && notePath !== ""),
  });
}

/** Invalidation: a human annotation changes only this note's view. */
export function useAnnotateWorkspaceNote(
  investigationId: string,
  revisionId: string,
  notePath: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: NoteAnnotationRequestV1) =>
      annotateWorkspaceNote(
        investigationId,
        revisionId,
        input,
        createIdempotencyKey(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceNotesKeys.view(investigationId, notePath),
      });
    },
  });
}

/**
 * Invalidation: a Bridge review appends an invocation and a consultation
 * ledger row; the note view, the ledger and the timeline change.
 */
export function useAskBridgeToReviewNote(
  investigationId: string,
  revisionId: string,
  notePath: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: AskBridgeRequestV1) =>
      askBridgeToReviewNote(
        investigationId,
        revisionId,
        input,
        createIdempotencyKey(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceNotesKeys.view(investigationId, notePath),
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

/**
 * Invalidation: an agent continuation enqueues a session instruction and
 * records its usage on the note; both projections change.
 */
export function useAskAgentToContinueFromNote(
  investigationId: string,
  revisionId: string,
  notePath: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: NoteAskAgentRequestV1) =>
      askAgentToContinueFromNote(
        investigationId,
        revisionId,
        input,
        createIdempotencyKey(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceNotesKeys.view(investigationId, notePath),
      });
      void queryClient.invalidateQueries({
        queryKey: agentSessionsKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.all(investigationId),
      });
    },
  });
}

/**
 * Invalidation: a verification task is created `proposed`, so the note view,
 * the task projections and the timeline change.
 */
export function useProposeNoteVerificationTask(
  investigationId: string,
  revisionId: string,
  notePath: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: NoteVerificationTaskRequestV1) =>
      proposeNoteVerificationTask(
        investigationId,
        revisionId,
        input,
        createIdempotencyKey(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceNotesKeys.view(investigationId, notePath),
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

/**
 * The observation proposal persists nothing, so no query is invalidated; the
 * human decides separately whether to import it through `contributions.ts`.
 */
export function useProposeNoteObservationConversion(
  investigationId: string,
  revisionId: string,
) {
  return useMutation({
    mutationFn: (input: NoteObservationProposalRequestV1) =>
      proposeNoteObservationConversion(investigationId, revisionId, input),
  });
}
