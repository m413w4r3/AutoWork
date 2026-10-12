export type Tlp = "CLEAR" | "GREEN" | "AMBER" | "AMBER+STRICT" | "RED";

export type EditionStatus = "open" | "archived";

export interface EditionFields {
  country: string;
  country_code: string;
  period_start: string;
  period_end: string;
  tlp: Tlp;
  languages: string[];
}

export interface Edition extends EditionFields {
  id: string;
  state: EditionStatus;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface Subject {
  id: string;
  edition_id: string;
  title: string;
  slug: string;
  tlp: Tlp;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface EditionPage {
  items: Edition[];
  total: number;
  page: number;
  page_size: number;
}

export type CompositionDisposition = "included" | "deferred" | "excluded";

export type CompositionPreparationStatus = "unassessed" | "ready" | "blocked";

export interface EditionCompositionEntry {
  id: string;
  subject_id: string;
  disposition: CompositionDisposition;
  position: number | null;
  production_run_id: string | null;
  pipeline_generation: number | null;
  artifact_id: string | null;
  artifact_version: number | null;
  artifact_input_hash: string | null;
  provenance_batch_id: string | null;
  review_decision_id: string | null;
  document_sha256: string | null;
  preparation_status: CompositionPreparationStatus;
}

export interface EditionComposition {
  id: string;
  edition_id: string;
  version: number;
  actor_id: string;
  preparation_status: CompositionPreparationStatus;
  created_at: string;
  updated_at: string;
  entries: EditionCompositionEntry[];
}

export interface CompositionReason {
  code: string;
  message: string;
  subject_id: string | null;
  details: Record<string, unknown> | null;
}

export interface EditionCompositionRead {
  edition_id: string;
  composition: EditionComposition | null;
  reasons: CompositionReason[];
  axes: {
    production_validity: boolean | null;
    discovery_update_available: boolean | null;
    composition_eligibility: boolean;
  };
}

export interface EditionCompositionEntryInput {
  subject_id: string;
  disposition: CompositionDisposition;
  position: number | null;
  production_run_id?: string | null;
  pipeline_generation?: number | null;
  artifact_id?: string | null;
  artifact_version?: number | null;
  artifact_input_hash?: string | null;
  provenance_batch_id?: string | null;
  review_decision_id?: string | null;
  document_sha256?: string | null;
}

export interface EditionCompositionPut {
  expected_version: number;
  entries: EditionCompositionEntryInput[];
}

export interface EditionCompositionExclusionResponse {
  composition: EditionComposition;
  decision_id: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status: number,
    public readonly details: Record<string, unknown> | null = null,
  ) {
    super(message);
  }
}

export async function listEditions(filters: {
  countryCode?: string;
  period?: string;
  state?: EditionStatus | "";
}): Promise<EditionPage> {
  const parameters = new URLSearchParams();
  if (filters.countryCode) parameters.set("country_code", filters.countryCode);
  if (filters.period) parameters.set("period", filters.period);
  if (filters.state) parameters.set("state", filters.state);
  return request<EditionPage>(`/api/editions?${parameters.toString()}`);
}

export function getEdition(editionId: string): Promise<Edition> {
  return request<Edition>(`/api/editions/${encodeURIComponent(editionId)}`);
}

export function listSubjects(editionId: string): Promise<Subject[]> {
  return request<Subject[]>(
    `/api/editions/${encodeURIComponent(editionId)}/subjects`,
  );
}

export function getEditionComposition(
  editionId: string,
): Promise<EditionCompositionRead> {
  return request(`/api/editions/${encodeURIComponent(editionId)}/composition`);
}

export function putEditionComposition(
  editionId: string,
  payload: EditionCompositionPut,
  idempotencyKey: string,
): Promise<EditionComposition> {
  return request(`/api/editions/${encodeURIComponent(editionId)}/composition`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(payload),
  });
}

export function excludeEditionCompositionSubject(
  editionId: string,
  subjectId: string,
  payload: { reason: string; expected_version: number },
  idempotencyKey: string,
): Promise<EditionCompositionExclusionResponse> {
  return request(
    `/api/editions/${encodeURIComponent(editionId)}/composition/exclusions/${encodeURIComponent(subjectId)}`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(payload),
    },
  );
}

export function getSubject(subjectId: string): Promise<Subject> {
  return request<Subject>(`/api/subjects/${encodeURIComponent(subjectId)}`);
}

export function updateSubjectMetadata(
  subjectId: string,
  payload: { version: number; title: string; tlp: Tlp },
): Promise<Subject> {
  return request<Subject>(`/api/subjects/${encodeURIComponent(subjectId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function createEdition(payload: EditionFields): Promise<Edition> {
  return request<Edition>("/api/editions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function archiveEdition(edition: Edition): Promise<Edition> {
  return request<Edition>(
    `/api/editions/${encodeURIComponent(edition.id)}/archive`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: edition.version }),
    },
  );
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (response.ok) {
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }
  const body = (await response.json().catch(() => null)) as {
    detail?: Record<string, unknown> | string;
  } | null;
  const detail = body?.detail;
  const message =
    typeof detail === "object" && typeof detail.message === "string"
      ? detail.message
      : "L’opération n’a pas pu être effectuée.";
  const code =
    typeof detail === "object" && typeof detail.code === "string"
      ? detail.code
      : "api_error";
  throw new ApiError(
    message,
    code,
    response.status,
    typeof detail === "object" ? detail : null,
  );
}
