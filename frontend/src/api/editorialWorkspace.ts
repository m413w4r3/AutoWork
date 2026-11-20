import { ApiError } from "./editions";

export type EditorialPublicationKind = "article" | "brief";
export type EditorialBlockKind =
  | "title"
  | "lead_paragraph"
  | "section_heading"
  | "paragraph"
  | "table"
  | "diagram"
  | "chart"
  | "figure";
export type EditorialRevisionStatus =
  "ready" | "proposal_pending" | "stale" | "needs_review";
export type EditorialImpactStatus =
  "none" | "pending" | "running" | "superseded" | "up_to_date" | "failed";
export type EditorialPdfStatus = "CURRENT" | "STALE";

export interface EditorialWorkspaceRevisionTarget {
  block_id: string;
  legacy_target_id: string | null;
}

export interface EditorialWorkspaceBlock {
  block_id: string;
  kind: EditorialBlockKind;
  parent_block_id: string | null;
  position: number;
  text: string | null;
  evidence_refs: Record<string, unknown>[];
  revision_target: EditorialWorkspaceRevisionTarget;
  before_hash: string | null;
  allowed_operations: string[];
  revision_status: EditorialRevisionStatus;
}

export interface EditorialDocumentOutlineNode {
  block_id: string;
  label: string;
  children: string[];
}

export interface EditorialWorkspaceCapabilities {
  can_discuss: boolean;
  can_preview_revision: boolean;
  manual_edit: boolean;
  can_add_enrichment: boolean;
  can_preview_pdf: boolean;
}

export interface EditorialWorkspaceSnapshot {
  subject_id: string;
  production_run_id: string;
  publication_kind: EditorialPublicationKind | null;
  synthesis_artifact_id: string;
  synthesis_version: number;
  synthesis_canonical_sha256: string;
  enrichment_artifact_id: string | null;
  enrichment_version: number | null;
  publication_artifact_id: string | null;
  publication_version: number | null;
  pipeline_generation: number;
  blocks: EditorialWorkspaceBlock[];
  enrichments: EditorialWorkspaceBlock[];
  document_outline: EditorialDocumentOutlineNode[];
  capabilities: EditorialWorkspaceCapabilities;
  preview_status: "IN_PROGRESS" | "READY" | "FAILED" | "STALE";
  impact_status: EditorialImpactStatus;
  pdf_status: EditorialPdfStatus;
  snapshot_id: string;
}

export interface EditorialServerDraft {
  id: string;
  subject_id: string;
  production_run_id: string;
  occurrence_id: string;
  base_snapshot_id: string;
  base_artifact_id: string;
  base_artifact_version: number;
  base_canonical_sha256: string;
  text_sha256: string;
  version: number;
  text: string;
  is_stale: boolean;
  created_at: string;
  updated_at: string;
  expires_at: string;
}

export interface ResolvedEditorialWorkspaceBlock {
  snapshot_id: string;
  block_id: string;
  path: string;
  before_hash: string;
  operation: string;
}

export type EditorialThreadBridgeStatus =
  | "ready"
  | "waiting_for_capacity"
  | "generating"
  | "reply_available"
  | "refused"
  | "session_lost"
  | "reconciliation_required";

export type EditorialThreadEventKind =
  | "user_message"
  | "bridge_reply"
  | "proposal"
  | "accepted"
  | "rejected"
  | "refused"
  | "reconciliation"
  | "generation";

export type EditorialThreadReconciliationOutcome =
  "reconciled_reply" | "not_submitted" | "still_unknown" | "session_lost";

export type EditorialThreadSendGeneration =
  | "queued"
  | "waiting_for_capacity"
  | "running"
  | "reconciliation_required"
  | "complete"
  | "failed"
  | "cancelled";

export interface EditorialThreadSendResult {
  kind: "accepted" | "already_known";
  thread_id: string;
  entry_id: string;
  generation: EditorialThreadSendGeneration;
}

export interface EditorialThreadBinding {
  subject_id: string;
  production_run_id: string;
  occurrence_id: string;
  artifact_id: string;
  artifact_version: number;
  canonical_sha256: string;
}

export interface EditorialThreadTimelineEvent {
  id: string;
  sequence: number;
  kind: EditorialThreadEventKind;
  actor_id: string | null;
  proposal_id: string | null;
  created_at: string;
  body: string | null;
  reconciliation_outcome?: EditorialThreadReconciliationOutcome | null;
}

interface EditorialThreadReadBase {
  current: EditorialThreadBinding;
  bridge_status: EditorialThreadBridgeStatus;
  event_sequence: number;
  timeline: EditorialThreadTimelineEvent[];
  etag: string;
}

export interface EditorialThreadUnopenedRead extends EditorialThreadReadBase {
  kind: "unopened";
  binding: EditorialThreadBinding;
}

export interface EditorialThreadOpenRead extends EditorialThreadReadBase {
  kind: "open";
  thread_id: string;
  origin: EditorialThreadBinding;
  binding_state: "valid";
  previous_sessions: number[];
}

export interface EditorialThreadStaleRead extends EditorialThreadReadBase {
  kind: "stale";
  thread_id: string | null;
  origin: EditorialThreadBinding | null;
  binding_state: "stale" | "ambiguous";
}

export type EditorialThreadRead =
  | EditorialThreadUnopenedRead
  | EditorialThreadOpenRead
  | EditorialThreadStaleRead;

export interface EditorialThreadReconciliationResult {
  thread_id: string;
  outcome: EditorialThreadReconciliationOutcome;
  message: string;
}

export interface EditorialThreadSessionStartResult {
  kind: "session_started";
  thread_id: string;
  session_number: number;
}

export type EditorialProposalState =
  | "preparing"
  | "proposed"
  | "refused"
  | "needs_review"
  | "accepted"
  | "rejected"
  | "stale";

export type EditorialProposalRefusalCode = "revision_not_applicable";

export type EditorialProposalOperation =
  | "rewrite_title"
  | "rewrite_lead"
  | "rewrite_paragraph"
  | "insert_paragraph"
  | "delete_paragraph"
  | "rewrite_section"
  | "insert_section"
  | "delete_section"
  | "merge_sections"
  | "split_section"
  | "rename_section"
  | "create_table"
  | "rewrite_table"
  | "delete_table"
  | "create_diagram"
  | "rewrite_diagram"
  | "delete_diagram"
  | "create_chart"
  | "rewrite_chart"
  | "delete_chart"
  | "create_source_figure"
  | "rewrite_source_figure"
  | "replace_source_figure"
  | "delete_source_figure";

export interface EditorialProposalPatch {
  schema_version: "editorial-patch-v1";
  operation: EditorialProposalOperation;
  target: Record<string, unknown>;
  target_id: string;
  occurrence_id: string;
  before_hash: string;
  rationale: string;
  after?: string;
  enrichment_patch?: Record<string, unknown>;
}

export interface EditorialProposalRead {
  id: string;
  subject_id: string;
  production_run_id: string;
  occurrence_id: string;
  thread_id: string | null;
  state: EditorialProposalState;
  state_version: number;
  operation: EditorialProposalOperation;
  base_artifact_id: string;
  base_artifact_version: number;
  base_canonical_sha256: string;
  base_text: string | null;
  patch_sha256: string | null;
  patch: EditorialProposalPatch | null;
  diagnostics: Record<string, unknown>;
  created_at: string;
}

export interface EditorialProposalDecisionResult {
  id: string;
  state: "rejected";
  state_version: number;
}

const EDITORIAL_THREAD_TRANSPORT_UNCERTAIN_MESSAGE =
  "L’état de votre envoi est incertain. Consultez le fil avant de continuer.";

const ERROR_MESSAGES: Record<string, string> = {
  publication_kind_unresolved:
    "Le type de publication est indéterminé. L’atelier reste en lecture seule.",
  editorial_snapshot_stale: "Le document a changé. Rechargez l’atelier.",
  editorial_identity_ambiguous:
    "L’identité de ce bloc historique est ambiguë. Il reste en lecture seule.",
  editorial_block_missing: "Ce bloc n’existe plus dans cette version.",
  editorial_before_hash_mismatch:
    "Ce bloc a changé. Rechargez l’atelier avant de réessayer.",
  editorial_anchor_stale:
    "Le paragraphe d’ancrage a changé ou a été supprimé. Repositionnez l’élément.",
  editorial_registry_conflict:
    "La continuité de ce bloc ne peut pas être prouvée. Une revue est nécessaire.",
  thread_not_found: "Discussion introuvable.",
  thread_binding_stale: "Ce paragraphe a changé. Rechargez l’atelier.",
  occurrence_ambiguous: "L’identité de ce paragraphe historique est ambiguë.",
  thread_body_unavailable:
    "Le contenu de cette discussion est temporairement indisponible.",
  thread_body_hash_mismatch:
    "Le contenu de cette discussion ne correspond plus à son empreinte.",
  proposal_not_found: "Proposition introuvable.",
  proposal_patch_unavailable:
    "Le contenu de cette proposition est temporairement indisponible.",
  proposal_patch_hash_mismatch:
    "Le contenu vérifié de cette proposition ne correspond plus.",
  proposal_base_unavailable:
    "Le texte de référence de cette proposition est indisponible.",
  proposal_base_hash_mismatch:
    "Le texte de référence ne correspond plus à son empreinte.",
  proposal_state_conflict:
    "La proposition a changé. Actualisez-la avant de décider.",
  proposal_idempotency_conflict:
    "Cette clé est déjà associée à une autre décision.",
  propose_not_enabled: "La proposition n’est pas encore activée.",
  editorial_thread_busy: "Une réponse est déjà en cours pour ce paragraphe.",
  external_llm_blocked: "La politique interdit l’envoi externe de ce contenu.",
  bridge_unavailable:
    "Le Bridge est indisponible. Actualisez avant de continuer.",
  reconciliation_required:
    "L’état de votre envoi est incertain. Consultez le fil avant de continuer.",
  idempotency_conflict: "Cette clé est déjà associée à une autre demande.",
  idempotency_key_required:
    "Une clé d’idempotence est requise pour envoyer ce message.",
  idempotency_key_invalid: "La clé d’idempotence doit être un UUID valide.",
  editorial_thread_session_not_lost:
    "La session actuelle est encore disponible. Aucune nouvelle discussion n’a été créée.",
  session_lost:
    "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre",
  editorial_message_invalid: "Saisissez un message avant de l’envoyer.",
  draft_not_found: "Aucun brouillon serveur n’est enregistré pour ce bloc.",
  draft_expired: "Ce brouillon a expiré. Rechargez le document.",
  draft_version_conflict:
    "Le brouillon a changé. Rechargez-le avant de continuer.",
  draft_base_stale:
    "Le document a changé depuis ce brouillon. Rechargez l’atelier.",
  draft_retention_unconfigured:
    "La conservation des brouillons n’est pas configurée.",
  draft_storage_unavailable:
    "Le stockage du brouillon est temporairement indisponible.",
  draft_cleanup_unavailable:
    "La purge du brouillon est temporairement indisponible.",
  draft_text_too_large: "Le brouillon dépasse la taille autorisée.",
  draft_if_match_required:
    "Une version de brouillon est requise pour cette modification.",
  draft_if_match_invalid: "La version de brouillon est invalide.",
  draft_request_invalid: "La requête de brouillon est invalide.",
  editorial_thread_transport_uncertain:
    EDITORIAL_THREAD_TRANSPORT_UNCERTAIN_MESSAGE,
};

interface ErrorDetail extends Record<string, unknown> {
  code?: string;
  message?: string;
  expires_at?: string;
}

async function adaptApiError(response: Response): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  const detail =
    typeof body === "object" && body !== null && "detail" in body
      ? body.detail
      : null;
  const detailObject =
    typeof detail === "object" && detail !== null
      ? (detail as ErrorDetail)
      : null;
  const code = detailObject?.code ?? "editorial_workspace_error";
  const message =
    ERROR_MESSAGES[code] ??
    detailObject?.message ??
    "L’atelier éditorial est indisponible.";
  return new ApiError(message, code, response.status, detailObject);
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw await adaptApiError(response);
  return (await response.json()) as T;
}

export function getEditorialServerDraft(
  subjectId: string,
  occurrenceId: string,
): Promise<EditorialServerDraft> {
  return request<EditorialServerDraft>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(occurrenceId)}/draft`,
    { cache: "no-store" },
  );
}

export function putEditorialServerDraft(
  subjectId: string,
  occurrenceId: string,
  input: {
    snapshot_id: string;
    text: string;
    expected_version: number;
  },
): Promise<EditorialServerDraft> {
  return request<EditorialServerDraft>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(occurrenceId)}/draft`,
    {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "If-Match": `"${input.expected_version}"`,
      },
      body: JSON.stringify({
        snapshot_id: input.snapshot_id,
        text: input.text,
      }),
      cache: "no-store",
    },
  );
}

export async function deleteEditorialServerDraft(
  subjectId: string,
  occurrenceId: string,
  expectedVersion: number,
): Promise<void> {
  const response = await fetch(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(occurrenceId)}/draft`,
    {
      method: "DELETE",
      headers: { "If-Match": `"${expectedVersion}"` },
      cache: "no-store",
    },
  );
  if (!response.ok) throw await adaptApiError(response);
}

export function getEditorialWorkspaceSnapshot(
  subjectId: string,
): Promise<EditorialWorkspaceSnapshot> {
  return request<EditorialWorkspaceSnapshot>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/snapshot`,
  );
}

export function getEditorialProposal(
  subjectId: string,
  occurrenceId: string,
  proposalId: string,
): Promise<EditorialProposalRead> {
  return request<EditorialProposalRead>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/proposals/${encodeURIComponent(proposalId)}`,
  );
}

export function rejectEditorialProposal(
  subjectId: string,
  occurrenceId: string,
  proposalId: string,
  decision: { expected_state_version: number; patch_sha256: string },
): Promise<EditorialProposalDecisionResult> {
  return request<EditorialProposalDecisionResult>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/proposals/${encodeURIComponent(proposalId)}/reject`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify(decision),
    },
  );
}

export function resolveEditorialWorkspaceBlock(
  subjectId: string,
  intent: { snapshot_id: string; block_id: string; operation: string },
): Promise<ResolvedEditorialWorkspaceBlock> {
  return request<ResolvedEditorialWorkspaceBlock>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        snapshot_id: intent.snapshot_id,
        intent: { block_id: intent.block_id, operation: intent.operation },
      }),
    },
  );
}

export async function getEditorialThread(
  subjectId: string,
  occurrenceId: string,
  options: { after_sequence?: number; etag?: string } = {},
): Promise<EditorialThreadRead | null> {
  const parameters = new URLSearchParams();
  if (options.after_sequence !== undefined) {
    parameters.set("after_sequence", String(options.after_sequence));
  }
  const query = parameters.size > 0 ? `?${parameters.toString()}` : "";
  const headers = new Headers();
  if (options.etag) headers.set("If-None-Match", options.etag);
  const response = await fetch(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}${query}`,
    { headers },
  );
  if (response.status === 304) return null;
  if (!response.ok) throw await adaptApiError(response);
  const payload = (await response.json()) as EditorialThreadRead;
  return { ...payload, etag: response.headers.get("ETag") ?? payload.etag };
}

export async function sendEditorialThreadMessage(
  subjectId: string,
  occurrenceId: string,
  text: string,
  kind: "message" | "propose" = "message",
): Promise<EditorialThreadSendResult> {
  const idempotencyKey = crypto.randomUUID();
  const url = `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/entries`;
  let transportAttempt = 0;
  while (true) {
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({ kind, text }),
      });
      if (!response.ok) throw await adaptApiError(response);
      return (await response.json()) as EditorialThreadSendResult;
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
      if (transportAttempt >= 2) {
        throw new ApiError(
          EDITORIAL_THREAD_TRANSPORT_UNCERTAIN_MESSAGE,
          "editorial_thread_transport_uncertain",
          0,
        );
      }
      transportAttempt += 1;
      await new Promise((resolve) =>
        setTimeout(resolve, 100 * transportAttempt),
      );
    }
  }
}

export async function reconcileEditorialThread(
  subjectId: string,
  occurrenceId: string,
): Promise<EditorialThreadReconciliationResult> {
  return request<EditorialThreadReconciliationResult>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/reconcile`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    },
  );
}

export async function startNewEditorialThreadSession(
  subjectId: string,
  occurrenceId: string,
  idempotencyKey: string,
): Promise<EditorialThreadSessionStartResult> {
  return request<EditorialThreadSessionStartResult>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/entries`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({ kind: "message", start_new_session: true }),
    },
  );
}
