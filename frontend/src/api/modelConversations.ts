import { ApiError } from "./editions";

export type ConversationPurpose =
  "discovery" | "analyst_assistance" | "pivot_research" | "subject_research" | "drafting" | "critic";

export interface ModelConversation {
  id: string;
  provider: "openai" | "qwen" | "fake";
  transport: "chatgpt_bridge" | "openai_responses" | "application_managed";
  purpose: ConversationPurpose;
  subject_id: string | null;
  title: string;
  status: string;
  requested_model: string | null;
  expected_profile: string | null;
  turn_count: number;
  last_used_at: string | null;
  evidence_warning: "not_primary_evidence";
}

export interface ModelConversationTurn {
  id: string;
  sequence: number;
  model_run_id: string;
  correlation_id: string;
  status: string;
  input_text: string | null;
  output_text: string | null;
  error: { code: string; message: string } | null;
}

export function listModelConversations(
  subjectId: string,
): Promise<ModelConversation[]> {
  return request(
    `/api/model-conversations?subject_id=${encodeURIComponent(subjectId)}`,
  );
}

export function createModelConversation(payload: {
  subject_id: string;
  title: string;
  purpose: ConversationPurpose;
  provider: "openai" | "qwen";
  expected_profile: string | null;
  requested_model: string | null;
}): Promise<ModelConversation> {
  return request("/api/model-conversations", jsonRequest(payload));
}

export function listConversationTurns(
  conversationId: string,
  subjectId: string,
): Promise<ModelConversationTurn[]> {
  return request(
    `/api/model-conversations/${encodeURIComponent(conversationId)}/turns?subject_id=${encodeURIComponent(subjectId)}`,
  );
}

export function addConversationTurn(
  conversationId: string,
  subjectId: string,
  payload: {
    message: string;
    mode: "fresh" | "continue";
    external_llm_allowed: boolean;
    idempotency_key: string;
  },
): Promise<ModelConversationTurn> {
  return request(
    `/api/model-conversations/${encodeURIComponent(conversationId)}/turns?subject_id=${encodeURIComponent(subjectId)}`,
    jsonRequest(payload),
  );
}

export function archiveModelConversation(
  conversationId: string,
  subjectId: string,
): Promise<ModelConversation> {
  return request(
    `/api/model-conversations/${encodeURIComponent(conversationId)}/archive?subject_id=${encodeURIComponent(subjectId)}`,
    { method: "POST" },
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
    detail?: string | { code?: string; message?: string };
  } | null;
  const detail = body?.detail;
  throw new ApiError(
    typeof detail === "string"
      ? detail
      : detail?.message || "La conversation modèle est inaccessible.",
    typeof detail === "object"
      ? detail?.code || "conversation_error"
      : "conversation_error",
    response.status,
  );
}
