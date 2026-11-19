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
  snapshot_id: string;
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
  editorial_thread_transport_uncertain:
    EDITORIAL_THREAD_TRANSPORT_UNCERTAIN_MESSAGE,
};

interface ErrorDetail {
  code?: string;
  message?: string;
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
  return new ApiError(message, code, response.status);
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw await adaptApiError(response);
  return (await response.json()) as T;
}

export function getEditorialWorkspaceSnapshot(
  subjectId: string,
): Promise<EditorialWorkspaceSnapshot> {
  return request<EditorialWorkspaceSnapshot>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/snapshot`,
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
        body: JSON.stringify({ kind: "message", text }),
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
