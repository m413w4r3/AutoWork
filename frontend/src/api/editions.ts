export type Tlp = "CLEAR" | "GREEN" | "AMBER" | "AMBER+STRICT" | "RED";

export type EditionStatus =
  | "draft"
  | "discovery"
  | "selection"
  | "production"
  | "review"
  | "assembling"
  | "published"
  | "archived";

export interface EditionFields {
  country: string;
  country_code: string;
  period_start: string;
  period_end: string;
  tlp: Tlp;
  languages: string[];
  target_articles: number;
  previous_edition_id: string | null;
  source_profile: string;
}

export interface Edition extends EditionFields {
  id: string;
  status: EditionStatus;
  version: number;
  progress_percent: number;
  allowed_transitions: EditionStatus[];
  created_at: string;
  updated_at: string;
}

export interface EditionPage {
  items: Edition[];
  total: number;
  page: number;
  page_size: number;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

export async function listEditions(filters: {
  countryCode?: string;
  period?: string;
  status?: EditionStatus | "";
}): Promise<EditionPage> {
  const parameters = new URLSearchParams();
  if (filters.countryCode) parameters.set("country_code", filters.countryCode);
  if (filters.period) parameters.set("period", filters.period);
  if (filters.status) parameters.set("status", filters.status);
  return request<EditionPage>(`/api/editions?${parameters.toString()}`);
}

export function getEdition(editionId: string): Promise<Edition> {
  return request<Edition>(`/api/editions/${encodeURIComponent(editionId)}`);
}

export function createEdition(payload: EditionFields): Promise<Edition> {
  return request<Edition>("/api/editions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function transitionEdition(
  edition: Edition,
  targetStatus: EditionStatus,
): Promise<Edition> {
  return request<Edition>(
    `/api/editions/${encodeURIComponent(edition.id)}/transitions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_status: targetStatus,
        version: edition.version,
      }),
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
    detail?: { code?: string; message?: string } | string;
  } | null;
  const detail = body?.detail;
  const message =
    typeof detail === "object" && detail?.message
      ? detail.message
      : "L’opération n’a pas pu être effectuée.";
  const code =
    typeof detail === "object" && detail?.code ? detail.code : "api_error";
  throw new ApiError(message, code, response.status);
}
