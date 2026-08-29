import { ApiError } from "./editions";
import type { PublicationDocument } from "./production";

export interface SubjectContentResponse {
  subject_id: string;
  run_id: string;
  pipeline_generation: number;
  artifact_id: string;
  artifact_version: number;
  artifact_input_hash: string;
  status: string;
  schema_version: string;
  canonical_content: PublicationDocument;
  rendered_content: string | null;
}

export interface SubjectIndicatorResponse {
  id: string;
  artifact_type: string;
  display_value: string;
  normalized_value: string;
  indicator_status: string;
  source_ids: string[];
}

export interface SubjectAssetResponse {
  id: string;
  original_name: string;
  mime_type: string | null;
  sha256: string | null;
  size: number | null;
  origin: string;
  provenance: Record<string, string> | null;
  tlp: string;
  do_not_submit: boolean;
  external_llm_allowed: boolean;
}

export interface SubjectAssetsResponse {
  sources: SubjectAssetResponse[];
  samples: SubjectAssetResponse[];
}

export function getSubjectContent(
  subjectId: string,
): Promise<SubjectContentResponse | null> {
  return requestOrNull(
    `/api/subjects/${encodeURIComponent(subjectId)}/content`,
  );
}

export function getSubjectIndicators(
  subjectId: string,
): Promise<SubjectIndicatorResponse[]> {
  return request(`/api/subjects/${encodeURIComponent(subjectId)}/indicators`);
}

export function getSubjectAssets(
  subjectId: string,
): Promise<SubjectAssetsResponse | null> {
  return requestOrNull(`/api/subjects/${encodeURIComponent(subjectId)}/assets`);
}

async function request<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (response.ok) return (await response.json()) as T;
  throw await apiError(response);
}

async function requestOrNull<T>(url: string): Promise<T | null> {
  const response = await fetch(url);
  if (response.status === 404) return null;
  if (response.ok) return (await response.json()) as T;
  throw await apiError(response);
}

async function apiError(response: Response): Promise<ApiError> {
  const body = (await response.json().catch(() => null)) as {
    detail?: { code?: string; message?: string } | string;
  } | null;
  const detail = body?.detail;
  const message =
    typeof detail === "string"
      ? detail
      : detail?.message || "Le contenu du sujet est inaccessible.";
  return new ApiError(
    message,
    typeof detail === "object" && detail?.code
      ? detail.code
      : "subject_content_error",
    response.status,
  );
}
