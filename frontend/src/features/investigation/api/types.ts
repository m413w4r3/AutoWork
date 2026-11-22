/**
 * Shared wire-level primitives of the M24 workshop API contract.
 *
 * Every union below is transcribed from the backend enum it mirrors; the
 * source module is named on each type. These are types only: no runtime enum
 * is exported, so a value always comes from a parsed response.
 */

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

/** ISO-8601 timestamp serialised by FastAPI from a `datetime`. */
export type IsoTimestamp = string;

/** Canonical UUID rendered as text. */
export type Uuid = string;

/**
 * Error codes every workshop route can return besides its own typed refusals:
 * the shared identity gate, the signed-session gate, the request helper's
 * fallback for a FastAPI validation body, and the browser transport itself.
 */
export type CommonErrorCode =
  | "verified_human_operator_required"
  | "agent_session_required"
  | "agent_session_signature_invalid"
  | "agent_session_binding_mismatch"
  | "agent_session_inactive"
  | "agent_session_lease_stale"
  | "agent_sessions_unavailable"
  | "agent_sessions_require_authenticated_identity"
  | "investigation_not_found"
  | "idempotency_conflict"
  | "investigation_api_error"
  | "network_error";

/** `domain/investigation_task.py::TaskActorKind`. */
export type TaskActorKind = "human" | "operator" | "system";

/** `domain/investigation_task.py::TaskStatus`. */
export type TaskStatus =
  | "proposed"
  | "ready"
  | "assigned"
  | "running"
  | "awaiting_input"
  | "awaiting_review"
  | "blocked"
  | "completed"
  | "cancelled"
  | "failed";

/** `domain/investigation_task.py::TaskPriority`. */
export type TaskPriority = "critical" | "high" | "normal" | "low";

/** `domain/investigation_task.py::ResultVerificationStatus`. */
export type ResultVerificationStatus =
  "unverified" | "pending_review" | "accepted" | "rejected";

/** `domain/investigation_task.py::BlockedReason`. */
export type BlockedReason =
  | "dependency"
  | "capability"
  | "approval"
  | "session_lost"
  | "lease_expired"
  | "input_unavailable"
  | "external";

/** `application/investigation_task_events.py::TaskEventType`. */
export type TaskEventType =
  | "task_created"
  | "status_changed"
  | "assigned"
  | "lease_acquired"
  | "lease_renewed"
  | "lease_released"
  | "lease_expired"
  | "result_submitted"
  | "verification_recorded"
  | "session_started"
  | "session_state_changed"
  | "handoff_created"
  | "contribution_imported";

/** `domain/investigation.py::ToolInvocationStatus`. */
export type ToolInvocationStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "empty"
  | "unavailable"
  | "policy_denied"
  | "needs_review"
  | "failed"
  | "partial";

/** `domain/investigation.py::ToolInvocationActorKind`. */
export type ToolInvocationActorKind = "human" | "operator";

/** `domain/investigation.py::BudgetDimension`. */
export type BudgetDimension =
  | "bridge_turns"
  | "vt_units"
  | "shodan_credits"
  | "downloads"
  | "tool_seconds"
  | "context_tokens"
  | "context_bytes";

/** `domain/investigation.py::TLPLevel`. */
export type TLPLevel = "white" | "clear" | "green" | "amber" | "red";

/** `domain/investigation.py::SampleOrigin`. */
export type SampleOrigin =
  "seed" | "vt_pivot" | "relationship" | "behavior" | "manual";

/** `domain/investigation.py::SampleQualification`. */
export type SampleQualification =
  "candidate" | "related" | "confirmed" | "rejected" | "uncertain";

/** `domain/entities.py::SampleState`. */
export type SampleState =
  "quarantined" | "review_candidate" | "validated" | "rejected";

/** `domain/investigation.py::FindingKind`. */
export type FindingKind =
  | "new_sample"
  | "infection_chain"
  | "capability"
  | "network_behaviour"
  | "claim_verification"
  | "detection";

/** `domain/investigation.py::FindingAssessment`. */
export type FindingAssessment =
  "supported" | "refuted" | "qualified" | "inconclusive";

/** `domain/investigation.py::NoveltyClass`. */
export type FindingNovelty =
  | "new_to_article"
  | "already_documented_but_new_detail"
  | "claim_correction"
  | "not_confirmed";

/** `domain/agentic_operator.py::OperatorAdapterKind`. */
export type OperatorAdapterKind = "codex_cli" | "claude_code";

/** `domain/agentic_operator.py::AgentSessionState`. */
export type AgentSessionState =
  | "provisioning"
  | "ready"
  | "running"
  | "awaiting_instruction"
  | "interrupt_requested"
  | "stopped"
  | "lost"
  | "reconciling"
  | "closed"
  | "failed";

/** `domain/agentic_operator.py::InstructionState`. */
export type InstructionState =
  "queued" | "delivered" | "acknowledged" | "cancelled";

/** `application/investigation_workspace.py::WorkspaceOrigin`. */
export type WorkspaceOrigin = "filesystem" | "ui" | "agent" | "system";

/** `domain/investigation.py::EvidenceKind` (observation vocabulary). */
export type EvidenceKind =
  | "vt_file_report"
  | "vt_relationship"
  | "vt_behaviour"
  | "shodan_observation"
  | "binary_analysis"
  | "binary_string"
  | "yara_match"
  | "negative_result"
  | "claim_source"
  | "other";

/** `domain/investigation_handoff.py::HandoffActorKind`. */
export type HandoffActorKind = "human" | "operator" | "system";

/** `domain/investigation_handoff.py::HandoffRefKind`. */
export type HandoffRefKind =
  "observation" | "finding" | "decision" | "consultation" | "file";

/** `domain/investigation_handoff.py::HandoffFreshnessKind`. */
export type HandoffFreshnessKind = HandoffRefKind;

/** `domain/investigation_handoff.py::HandoffFreshnessState`. */
export type HandoffFreshnessState = "fresh" | "stale";
