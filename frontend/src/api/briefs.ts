import { ApiError } from "./editions";

export interface BriefSentence {
  id: string;
  text: string;
  factual: boolean;
  claim_ids: string[];
  indicator_ids: string[];
  evidence: Array<{
    id: string;
    kind: string;
    value: string;
    source_id: string;
    source_span: { start: number; end: number };
  }>;
}

export interface BriefBlock {
  id: string;
  sentences: BriefSentence[];
}

export interface BriefResult {
  subject_id: string;
  pack: null | {
    id: string;
    version: number;
    content_hash: string;
    source_count: number;
    claim_count: number;
    indicator_count: number;
    entity_count: number;
    uncertainty_count: number;
  };
  draft: null | {
    id: string;
    version: number;
    title: string;
    provider: string;
    stale: boolean;
  };
  blocks: BriefBlock[];
  limits: string[];
  references: Array<{ id: string; origin: string; sha256: string }>;
  versions: Array<{
    id: string;
    version: number;
    title: string;
    provider: string;
    stale: boolean;
  }>;
  status:
    "empty" | "draft" | "changes_requested" | "approved" | "promoted" | "stale";
  qa: Record<string, boolean>;
  qa_errors: string[];
  diff: string;
}

export const getBrief = (subjectId: string): Promise<BriefResult> =>
  request(`/api/subjects/${encodeURIComponent(subjectId)}/brief`);

export const freezeBriefPack = (subjectId: string): Promise<BriefResult> =>
  request(`/api/subjects/${encodeURIComponent(subjectId)}/brief/freeze`, {
    method: "POST",
  });

export const generateBrief = (
  subjectId: string,
  provider: "qwen" | "openai",
): Promise<{ job_id: string; duplicate: boolean }> =>
  request(
    `/api/subjects/${encodeURIComponent(subjectId)}/brief/generate`,
    json({ provider }),
  );

export const regenerateBriefBlock = (
  subjectId: string,
  blockId: string,
  provider: "qwen" | "openai",
  instruction?: string,
): Promise<{ job_id: string; duplicate: boolean }> =>
  request(
    `/api/subjects/${encodeURIComponent(subjectId)}/brief/blocks/${encodeURIComponent(blockId)}/regenerate`,
    json({ provider, instruction }),
  );

export const editBriefBlock = (
  subjectId: string,
  blockId: string,
  sentenceTexts: string[],
): Promise<BriefResult> =>
  request(
    `/api/subjects/${encodeURIComponent(subjectId)}/brief/blocks/${encodeURIComponent(blockId)}`,
    json({ sentence_texts: sentenceTexts }, "PATCH"),
  );

export const requestBriefChanges = (
  subjectId: string,
  note: string,
): Promise<BriefResult> =>
  request(
    `/api/subjects/${encodeURIComponent(subjectId)}/brief/request-changes`,
    json({ note }),
  );

export const approveBrief = (subjectId: string): Promise<BriefResult> =>
  request(`/api/subjects/${encodeURIComponent(subjectId)}/brief/approve`, {
    method: "POST",
  });

export const promoteBrief = (subjectId: string): Promise<BriefResult> =>
  request(`/api/subjects/${encodeURIComponent(subjectId)}/brief/promote`, {
    method: "POST",
  });

export const briefMarkdownUrl = (subjectId: string) =>
  `/api/subjects/${encodeURIComponent(subjectId)}/brief/export.md`;

function json(body: object, method = "POST"): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (response.ok) return (await response.json()) as T;
  const body = (await response.json().catch(() => null)) as {
    detail?: string;
  } | null;
  throw new ApiError(
    body?.detail ?? "La brève n’a pas pu être mise à jour.",
    "brief_error",
    response.status,
  );
}
