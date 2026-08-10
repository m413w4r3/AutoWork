import { ApiError } from "./editions";

export type CollectionState =
  | "queued"
  | "fetching"
  | "archived"
  | "extracted"
  | "completed"
  | "unavailable"
  | "blocked"
  | "failed_retryable"
  | "failed_terminal";

export interface CollectionAttempt {
  id: string;
  requested_url: string;
  final_url: string | null;
  redirect_chain: string[];
  attempted_at: string;
  completed_at: string;
  http_status: number | null;
  declared_content_type: string | null;
  detected_content_type: string | null;
  encoded_size: number | null;
  encoded_sha256: string | null;
  decoded_size: number | null;
  decoded_sha256: string | null;
  content_encoding: string | null;
  outcome: string;
  failure_reason: string | null;
}

export interface CollectedSource {
  id: string;
  requested_url: string;
  state: CollectionState;
  proposed_role: SourceRole;
  relationship_status: "provisional" | "verified";
  relationship_evidence: string;
  source_document_id: string | null;
  attempt_count: number;
  error_reason: string | null;
  fetch_lease_expires_at: string | null;
  latest_attempt: CollectionAttempt | null;
}

export type SourceRole =
  "primary" | "independent" | "relay" | "aggregator" | "social" | "unknown";

export interface EvidenceClaim {
  id: string;
  kind: string;
  original_value: string;
  current_value: string;
  status: "extracted" | "validated" | "corrected" | "rejected";
  source_id: string;
  source_span: { start: number; end: number };
  passage: string;
  extraction_payload: Record<string, unknown>;
}

export interface EvidenceIndicator {
  id: string;
  kind: string;
  original_value: string;
  normalized_value: string;
  current_value: string;
  status: "extracted" | "validated" | "corrected" | "rejected";
  source_id: string;
  source_span: { start: number; end: number };
}

export interface SubjectWorkbenchResult {
  subject_id: string;
  sources: CollectedSource[];
  claims: EvidenceClaim[];
  indicators: EvidenceIndicator[];
}

export function getSubjectWorkbench(
  subjectId: string,
): Promise<SubjectWorkbenchResult> {
  return request(`/api/subjects/${encodeURIComponent(subjectId)}/workbench`);
}

export function launchSubjectCollection(
  subjectId: string,
): Promise<{ job_id: string; duplicate: boolean }> {
  return request(`/api/subjects/${encodeURIComponent(subjectId)}/collection`, {
    method: "POST",
  });
}

export function retryCollectedSource(
  subjectId: string,
  sourceId: string,
): Promise<{ job_id: string; duplicate: boolean }> {
  return request(
    `/api/subjects/${encodeURIComponent(subjectId)}/sources/${encodeURIComponent(sourceId)}/retry`,
    { method: "POST" },
  );
}

export function decideSourceRelationship(
  subjectId: string,
  sourceId: string,
  role: SourceRole,
): Promise<CollectedSource> {
  return request(
    `/api/subjects/${encodeURIComponent(subjectId)}/sources/${encodeURIComponent(sourceId)}/relationship`,
    jsonRequest({ role }),
  );
}

export function reviewClaim(
  subjectId: string,
  claimId: string,
  action: "validate" | "correct" | "reject",
  correctedValue?: string,
): Promise<EvidenceClaim> {
  return request(
    `/api/subjects/${encodeURIComponent(subjectId)}/claims/${encodeURIComponent(claimId)}/decision`,
    jsonRequest({ action, corrected_value: correctedValue }),
  );
}

export function reviewIndicator(
  subjectId: string,
  indicatorId: string,
  action: "validate" | "correct" | "reject",
  correctedValue?: string,
): Promise<EvidenceIndicator> {
  return request(
    `/api/subjects/${encodeURIComponent(subjectId)}/indicators/${encodeURIComponent(indicatorId)}/decision`,
    jsonRequest({ action, corrected_value: correctedValue }),
  );
}

function jsonRequest(body: object): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (response.ok) return (await response.json()) as T;
  const body = (await response.json().catch(() => null)) as {
    detail?: string | { message?: string };
  } | null;
  const message =
    typeof body?.detail === "string"
      ? body.detail
      : body?.detail?.message ||
        "Le workbench sujet n’a pas pu être mis à jour.";
  throw new ApiError(message, "subject_workbench_error", response.status);
}
