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
