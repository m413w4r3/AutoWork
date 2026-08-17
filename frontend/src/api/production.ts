/**
 * Frontend API client for subject production workflow
 */

export interface StageStatus {
  status: "pending" | "running" | "succeeded" | "needs_review" | "failed";
  version: number | null;
  error_code: string | null;
  error_message: string | null;
}

export interface ProductionStatus {
  subject_id: string;
  title: string;
  editorial_type: string;
  status:
    | "queued"
    | "running"
    | "ready"
    | "needs_review"
    | "failed"
    | "cancelled";
  current_stage: string;
  progress_current: number;
  progress_total: number;
  conversation_id: string | null;
  run_id: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  stages: Record<string, StageStatus>;
}

export interface BatchStatus {
  batch_id: string;
  edition_id: string;
  profile: string;
  status: "queued" | "running" | "completed" | "completed_with_issues" | "cancelled";
  items: number;
  completed: number;
  needs_review: number;
  failed: number;
  current_subject_index: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ArtifactResponse {
  artifact_id: string;
  stage: string;
  version: number;
  status: "verified" | "stale" | "needs_review";
  metadata: Record<string, unknown>;
}

/**
 * Start production of a subject
 */
export async function startSubjectProduction(
  subjectId: string,
  editionId: string,
  profile: "brief_auto" | "major_assisted" = "brief_auto"
): Promise<{ run_id: string; status: string }> {
  const response = await fetch(`/api/subjects/${subjectId}/production`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      profile,
      edition_id: editionId,
    }),
  });

  if (!response.ok) {
    throw new Error(`Failed to start production: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Get production status for a subject
 */
export async function getSubjectProduction(
  subjectId: string
): Promise<ProductionStatus> {
  const response = await fetch(`/api/subjects/${subjectId}/production`, {
    method: "GET",
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch production status: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Retry references generation
 */
export async function retryReferences(subjectId: string): Promise<{ status: string }> {
  const response = await fetch(
    `/api/subjects/${subjectId}/production/references/retry`,
    {
      method: "POST",
    }
  );

  if (!response.ok) {
    throw new Error(`Failed to retry references: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Retry synthesis generation
 */
export async function retrySynthesis(subjectId: string): Promise<{ status: string }> {
  const response = await fetch(
    `/api/subjects/${subjectId}/production/synthesis/retry`,
    {
      method: "POST",
    }
  );

  if (!response.ok) {
    throw new Error(`Failed to retry synthesis: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Cancel subject production
 */
export async function cancelSubjectProduction(
  subjectId: string
): Promise<{ status: string }> {
  const response = await fetch(`/api/subjects/${subjectId}/production/cancel`, {
    method: "POST",
  });

  if (!response.ok) {
    throw new Error(`Failed to cancel production: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Get references artifact
 */
export async function getReferencesArtifact(
  subjectId: string
): Promise<ArtifactResponse> {
  const response = await fetch(
    `/api/subjects/${subjectId}/production/artifacts/references`,
    { method: "GET" }
  );

  if (!response.ok) {
    throw new Error(`Failed to fetch references: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Get extraction artifact
 */
export async function getExtractionArtifact(
  subjectId: string
): Promise<ArtifactResponse> {
  const response = await fetch(
    `/api/subjects/${subjectId}/production/artifacts/extraction`,
    { method: "GET" }
  );

  if (!response.ok) {
    throw new Error(`Failed to fetch extraction: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Get synthesis artifact
 */
export async function getSynthesisArtifact(
  subjectId: string
): Promise<ArtifactResponse> {
  const response = await fetch(
    `/api/subjects/${subjectId}/production/artifacts/synthesis`,
    { method: "GET" }
  );

  if (!response.ok) {
    throw new Error(`Failed to fetch synthesis: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Get brief artifact
 */
export async function getBriefArtifact(
  subjectId: string
): Promise<ArtifactResponse> {
  const response = await fetch(
    `/api/subjects/${subjectId}/production/artifacts/brief`,
    { method: "GET" }
  );

  if (!response.ok) {
    throw new Error(`Failed to fetch brief: ${response.statusText}`);
  }

  return response.json();
}

// Edition Production API

/**
 * Start batch production for an edition
 */
export async function startEditionBriefProduction(
  editionId: string
): Promise<BatchStatus> {
  const response = await fetch(`/api/editions/${editionId}/production/briefs`, {
    method: "POST",
  });

  if (!response.ok) {
    throw new Error(`Failed to start batch production: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Get batch production status
 */
export async function getEditionBriefProduction(
  editionId: string
): Promise<BatchStatus> {
  const response = await fetch(`/api/editions/${editionId}/production/briefs`, {
    method: "GET",
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch batch status: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Cancel batch production
 */
export async function cancelEditionBatch(
  editionId: string,
  batchId: string
): Promise<{ status: string }> {
  const response = await fetch(
    `/api/editions/${editionId}/production/briefs/${batchId}/cancel`,
    {
      method: "POST",
    }
  );

  if (!response.ok) {
    throw new Error(`Failed to cancel batch: ${response.statusText}`);
  }

  return response.json();
}
