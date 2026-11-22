/**
 * Host workspace binding, sync, file views and note writes
 * (`api/investigation_workspace.py`, WS1).
 *
 * Reads degrade to `workspace_available: false` when the host agent is absent;
 * content reads and every write return the typed `503
 * workspace_local_unavailable` instead, and nothing is queued offline. The UI
 * note write (`PUT /workspace/files`) and the conflict resolution carry
 * `Idempotency-Key` (8–128 chars, `[A-Za-z0-9._:-]`) plus the exact CAS SHA.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { workspaceNotesKeys } from "./workspaceNotes";
import { tasksKeys } from "./tasks";
import {
  createIdempotencyKey,
  getJson,
  investigationPath,
  postEmpty,
  postJson,
  putJson,
  queryString,
} from "./client";
import type {
  CommonErrorCode,
  IsoTimestamp,
  Uuid,
  WorkspaceOrigin,
} from "./types";

export type WorkspaceAvailabilityCode =
  | "available"
  | "unconfigured"
  | "unavailable"
  | "identity_conflict"
  | "not_bound";
export type WorkspacePolicyClass =
  | "human_editable"
  | "agent_owned"
  | "generated_immutable"
  | "read_only"
  | "agent_internal";
export type WorkspaceImportStatus =
  "received" | "applied" | "ignored" | "rejected";
export type ConflictChoice = "keep_current" | "keep_proposed" | "merged_text";
export type ExportSource = "plan" | "vt" | "results";
export type ExportAction =
  "exported" | "already_exported" | "preserved" | "conflict";

export type WorkspaceErrorCode =
  | CommonErrorCode
  | "workspace_error"
  | "workspace_not_bound"
  | "workspace_identity_unavailable"
  | "workspace_synthesis_pin_mismatch"
  | "synthesis_not_verified"
  | "workspace_identity_conflict"
  | "workspace_sync_in_progress"
  | "workspace_sync_failed"
  | "workspace_file_not_found"
  | "workspace_content_mismatch"
  | "workspace_path_not_editable"
  | "workspace_file_too_large"
  | "workspace_revision_conflict"
  | "workspace_conflict_not_found"
  | "workspace_conflict_stale"
  | "workspace_conflict_already_resolved"
  | "workspace_export_refused"
  | "workspace_local_unavailable"
  | "invalid_response"
  | "invalid_request"
  | "invalid_path"
  | "unauthorized"
  | "workspace_not_found"
  | "workspace_unmanaged_directory"
  | "revision_conflict"
  | "concurrent_change"
  | "policy_denied"
  | "busy"
  | "length_required"
  | "size_limit"
  | "hash_mismatch"
  | "cursor_expired"
  | "not_implemented"
  | "internal_error";

export interface WorkspaceBindingV1 {
  workspace_id: string;
  investigation_id: Uuid;
  subject_id: Uuid;
  delivery_code: string;
  article_slug: string;
  synthesis_sha256: string;
  created_at: IsoTimestamp;
}

export interface WorkspaceManifestV1 {
  /** Serialised Pydantic alias of `manifest_schema`. */
  schema: "autowork-workspace-manifest.v1";
  workspace_id: string;
  delivery_code: string;
  article_slug: string;
  subject_id: Uuid;
  investigation_id: Uuid;
  synthesis_sha256: string;
  created_at: IsoTimestamp;
  created_by: string;
  layout_version: number;
  identity_sha256: string;
}

export interface WorkspaceEnsureResultV1 {
  binding: WorkspaceBindingV1;
  binding_created: boolean;
  workspace_created: boolean;
}

export interface WorkspaceFileVersionV1 {
  revision_id: string;
  sha256: string;
  size: number;
  observed_at: IsoTimestamp;
  origin: WorkspaceOrigin;
  task_id: Uuid | null;
  supersedes_revision_id: string | null;
}

export interface WorkspaceFileViewV1 {
  path: string;
  revision_id: string;
  sha256: string;
  size: number;
  mode: string;
  policy_class: WorkspacePolicyClass | null;
  origin: WorkspaceOrigin;
  import_status: WorkspaceImportStatus;
  task_id: Uuid | null;
  observed_at: IsoTimestamp;
  conflict: boolean;
  history: WorkspaceFileVersionV1[];
}

export interface WorkspaceFileListV1 {
  investigation_id: Uuid;
  workspace_id: string;
  workspace_available: boolean;
  files: WorkspaceFileViewV1[];
  next_page_token: string | null;
}

export interface WorkspaceContentViewV1 {
  investigation_id: Uuid;
  workspace_id: string;
  path: string;
  revision_id: string;
  sha256: string;
  size: number;
  metadata_only: boolean;
  text: string | null;
  content_base64: string | null;
}

export interface WorkspaceNoteWriteResultV1 {
  investigation_id: Uuid;
  workspace_id: string;
  path: string;
  revision: WorkspaceFileVersionV1;
  created: boolean;
  already_present: boolean;
}

export interface WorkspaceConflictVersionV1 {
  path: string;
  sha256: string | null;
  size: number | null;
  text: string | null;
  binary: boolean;
}

export interface WorkspaceConflictViewV1 {
  conflict_id: string;
  path: string;
  conflict_path: string | null;
  current_sha256: string | null;
  proposed_sha256: string;
  origin: WorkspaceOrigin;
  task_id: Uuid | null;
  observed_at: IsoTimestamp;
  current: WorkspaceConflictVersionV1 | null;
  proposed: WorkspaceConflictVersionV1 | null;
  diff: string | null;
}

export interface WorkspaceConflictListV1 {
  investigation_id: Uuid;
  workspace_id: string;
  workspace_available: boolean;
  conflicts: WorkspaceConflictViewV1[];
}

export interface WorkspaceConflictResolutionV1 {
  investigation_id: Uuid;
  workspace_id: string;
  conflict_id: string;
  path: string;
  choice: ConflictChoice;
  decision_id: Uuid;
  result_revision: WorkspaceFileVersionV1 | null;
  current: WorkspaceConflictVersionV1 | null;
  proposed: WorkspaceConflictVersionV1 | null;
  diff: string | null;
  idempotent_replay: boolean;
}

export interface WorkspaceSyncReportV1 {
  investigation_id: Uuid;
  workspace_id: string;
  generation: number;
  cursor: number;
  resync: boolean;
  truncated: boolean;
  files_observed: number;
  events_received: number;
  events_inserted: number;
  events_existing: number;
  revisions_inserted: number;
  new_human_notes: string[];
  changed_human_notes: string[];
  human_notes_truncated: boolean;
  conflicts_observed: number;
  unresolved_conflicts: number;
  policy_violations: number;
  lastly_observed_at: IsoTimestamp | null;
}

export interface WorkspaceExportEntryV1 {
  path: string;
  sha256: string;
  source: ExportSource;
  action: ExportAction;
  revision_id: string | null;
}

export interface WorkspaceExportReportV1 {
  investigation_id: Uuid;
  workspace_id: string;
  exported: number;
  already_exported: number;
  preserved: number;
  conflicts: number;
  vt_snapshot: "exported" | "none";
  entries: WorkspaceExportEntryV1[];
}

export interface WorkspaceCountsV1 {
  files: number;
  human_notes: number;
  unresolved_conflicts: number;
  policy_violations: number;
  plans: number;
  tasks: number;
  findings: number;
}

export interface WorkspaceStatusV1 {
  investigation_id: Uuid;
  workspace_state: "bound" | "unbound";
  workspace_available: boolean;
  availability_code: WorkspaceAvailabilityCode;
  availability_reason: string | null;
  binding: WorkspaceBindingV1 | null;
  manifest: WorkspaceManifestV1 | null;
  generation: number | null;
  cursor: number | null;
  counts: WorkspaceCountsV1;
  lastly_observed_at: IsoTimestamp | null;
}

export interface WorkspaceFileListParams {
  prefix?: string;
  page_token?: string;
  limit?: number;
}

export interface NoteWriteRequest {
  path: string;
  expected_sha256: string;
  text: string;
}

export interface ConflictResolveRequest {
  choice: ConflictChoice;
  expected_current_sha256: string;
  expected_proposed_sha256: string;
  merged_text?: string;
  reason?: string;
}

export const workspaceKeys = {
  all: (investigationId: string) =>
    ["investigation", investigationId, "workspace"] as const,
  status: (investigationId: string) =>
    [...workspaceKeys.all(investigationId), "status"] as const,
  files: (investigationId: string, params: WorkspaceFileListParams = {}) =>
    [
      ...workspaceKeys.all(investigationId),
      "files",
      params.prefix ?? "",
      params.page_token ?? "",
      params.limit ?? null,
    ] as const,
  content: (investigationId: string, path: string, expectedSha256?: string) =>
    [
      ...workspaceKeys.all(investigationId),
      "content",
      path,
      expectedSha256 ?? "",
    ] as const,
  conflicts: (investigationId: string) =>
    [...workspaceKeys.all(investigationId), "conflicts"] as const,
};

export function getWorkspaceStatus(
  investigationId: string,
  signal?: AbortSignal,
): Promise<WorkspaceStatusV1> {
  return getJson<WorkspaceStatusV1>(
    investigationPath(investigationId, "/workspace"),
    signal,
  );
}

export function ensureWorkspace(
  investigationId: string,
  signal?: AbortSignal,
): Promise<WorkspaceEnsureResultV1> {
  return postEmpty<WorkspaceEnsureResultV1>(
    investigationPath(investigationId, "/workspace/ensure"),
    undefined,
    signal,
  );
}

export function syncWorkspace(
  investigationId: string,
  signal?: AbortSignal,
): Promise<WorkspaceSyncReportV1> {
  return postEmpty<WorkspaceSyncReportV1>(
    investigationPath(investigationId, "/workspace/sync"),
    undefined,
    signal,
  );
}

export function exportWorkspace(
  investigationId: string,
  signal?: AbortSignal,
): Promise<WorkspaceExportReportV1> {
  return postEmpty<WorkspaceExportReportV1>(
    investigationPath(investigationId, "/workspace/exports"),
    undefined,
    signal,
  );
}

export function listWorkspaceFiles(
  investigationId: string,
  params: WorkspaceFileListParams = {},
  signal?: AbortSignal,
): Promise<WorkspaceFileListV1> {
  return getJson<WorkspaceFileListV1>(
    investigationPath(investigationId, "/workspace/files") +
      queryString({
        prefix: params.prefix,
        page_token: params.page_token,
        limit: params.limit,
      }),
    signal,
  );
}

export function readWorkspaceFile(
  investigationId: string,
  path: string,
  expectedSha256?: string,
  signal?: AbortSignal,
): Promise<WorkspaceContentViewV1> {
  return getJson<WorkspaceContentViewV1>(
    investigationPath(investigationId, "/workspace/files/content") +
      queryString({ path, expected_sha256: expectedSha256 }),
    signal,
  );
}

export function writeWorkspaceNote(
  investigationId: string,
  input: NoteWriteRequest,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<WorkspaceNoteWriteResultV1> {
  return putJson<WorkspaceNoteWriteResultV1>(
    investigationPath(investigationId, "/workspace/files"),
    input,
    idempotencyKey,
    signal,
  );
}

export function listWorkspaceConflicts(
  investigationId: string,
  signal?: AbortSignal,
): Promise<WorkspaceConflictListV1> {
  return getJson<WorkspaceConflictListV1>(
    investigationPath(investigationId, "/workspace/conflicts"),
    signal,
  );
}

export function resolveWorkspaceConflict(
  investigationId: string,
  conflictId: string,
  input: ConflictResolveRequest,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<WorkspaceConflictResolutionV1> {
  return postJson<WorkspaceConflictResolutionV1>(
    investigationPath(
      investigationId,
      `/workspace/conflicts/${encodeURIComponent(conflictId)}/resolve`,
    ),
    input,
    idempotencyKey,
    signal,
  );
}

export function useWorkspaceStatus(
  investigationId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: workspaceKeys.status(investigationId),
    queryFn: ({ signal }) => getWorkspaceStatus(investigationId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useWorkspaceFiles(
  investigationId: string,
  params: WorkspaceFileListParams = {},
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: workspaceKeys.files(investigationId, params),
    queryFn: ({ signal }) =>
      listWorkspaceFiles(investigationId, params, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

export function useWorkspaceFileContent(
  investigationId: string,
  path: string,
  expectedSha256?: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: workspaceKeys.content(investigationId, path, expectedSha256),
    queryFn: ({ signal }) =>
      readWorkspaceFile(investigationId, path, expectedSha256, signal),
    enabled: options.enabled ?? (investigationId !== "" && path !== ""),
  });
}

export function useWorkspaceConflicts(
  investigationId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: workspaceKeys.conflicts(investigationId),
    queryFn: ({ signal }) => listWorkspaceConflicts(investigationId, signal),
    enabled: options.enabled ?? investigationId !== "",
  });
}

/** Invalidation: ensure creates/rebinds the workspace descriptor and manifest. */
export function useEnsureWorkspace(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => ensureWorkspace(investigationId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceKeys.all(investigationId),
      });
    },
  });
}

/**
 * Invalidation: sync ingests file events/revisions and conflicts; the status
 * counts, file pages and conflict list change together.
 */
export function useSyncWorkspace(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => syncWorkspace(investigationId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceKeys.all(investigationId),
      });
    },
  });
}

/** Invalidation: exports write generated files, which changes file metadata. */
export function useExportWorkspace(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => exportWorkspace(investigationId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceKeys.all(investigationId),
      });
    },
  });
}

/**
 * Invalidation: a note write creates one revision, so the file pages, the
 * content read for that path, the conflict list and the note view are stale.
 */
export function useWriteWorkspaceNote(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: NoteWriteRequest) =>
      writeWorkspaceNote(investigationId, input, createIdempotencyKey()),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({
        queryKey: workspaceKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: workspaceNotesKeys.view(investigationId, result.path),
      });
    },
  });
}

/**
 * Invalidation: resolving a conflict writes a revision and an analyst
 * decision; the workspace projections and the timeline are stale.
 */
export function useResolveWorkspaceConflict(investigationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      conflictId,
      input,
    }: {
      conflictId: string;
      input: ConflictResolveRequest;
    }) =>
      resolveWorkspaceConflict(
        investigationId,
        conflictId,
        input,
        createIdempotencyKey(),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceKeys.all(investigationId),
      });
      void queryClient.invalidateQueries({
        queryKey: tasksKeys.timeline(investigationId),
      });
    },
  });
}
