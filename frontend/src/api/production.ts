import { ApiError, type EditionStatus } from "./editions";

export type ProductionRunStatus =
  "queued" | "running" | "ready" | "needs_review" | "failed" | "cancelled";

export type ProductionStage =
  | "sources"
  | "references"
  | "extraction"
  | "relevance_projection"
  | "synthesis"
  | "editorial_enrichment"
  | "assembly";

export type ProductionBatchPhase = "initial" | "recovery" | "review";
export type ProductionRecoveryDisposition = "auto" | "manual_only";

export type ExtractionProgressProfile = "full" | "ioc_rules";
/** Per-source verdict of the canonical EXTRACTION stage. */
export type ExtractionProgressSourceStatus =
  "pending" | "cached" | "succeeded" | "failed" | "omitted";

export interface ExtractionProgressSource {
  /** The exact ``source_document_id``, or the URL of an omitted source. */
  source_id: string;
  title: string | null;
  canonical_url: string;
  tier: "core" | "supporting" | "technical";
  /** ``null`` for a source the REFERENCES corpus left out of the plan. */
  profile: ExtractionProgressProfile | null;
  status: ExtractionProgressSourceStatus;
  reuse_state: "fresh" | "reused" | "content_duplicate" | null;
  ioc_count: number;
  rule_count: number;
}

export interface ExtractionProgress {
  total_sources: number;
  completed_sources: number;
  full_total: number;
  full_completed: number;
  ioc_rules_total: number;
  ioc_rules_completed: number;
  cache_hits: number;
  model_calls: number;
  skipped_sources: number;
  confirmed_iocs: number;
  contextual_iocs: number;
  rules_total: number;
  yara_rules: number;
  sigma_rules: number;
  suricata_rules: number;
  snort_rules: number;
  sources: ExtractionProgressSource[];
}

export interface ExtractionRejection {
  source_id: string;
  source_url: string;
  batch_id: string | null;
  model_run_id: string;
  proposal_index: number;
  proposal_kind: string;
  artifact_type: string | null;
  reason_code: string;
  value: string;
  value_hash: string;
}

export interface ExtractionRejections {
  q2_rejected_rules: ExtractionRejection[];
  q2_rejected_rule_count: number;
  q2_rejected_artifact_count: number;
  q2_source_evidence_rejections: ExtractionRejection[];
}

export type ProductionBatchStatus =
  "queued" | "running" | "completed" | "completed_with_issues" | "cancelled";

export interface StageStatus {
  status:
    | "pending"
    | "running"
    | "succeeded"
    | "needs_review"
    | "failed"
    | "cancelled";
  version: number | null;
  error_code: string | null;
  error_message: string | null;
  reused?: boolean;
  reused_from_artifact_id?: string | null;
  reused_from_created_at?: string | null;
  research_date?: string | null;
  /** Short user-facing progress detail, when the pipeline exposes one. */
  detail?: string;
  /** Only on the sources stage. */
  archived_sources?: number;
}

export interface ProductionResumePlan {
  previous_status: ProductionRunStatus;
  resume_from_stage: ProductionStage;
  /** Stages whose artifact the resume keeps instead of recomputing. */
  reused_artifacts: string[];
  /** Upper bound: a stage that runs may still hit a reusable checkpoint. */
  model_calls_expected: number;
}

export interface ProductionStatus {
  subject_id: string;
  edition_id: string;
  title: string;
  status: ProductionRunStatus;
  current_stage: ProductionStage;
  progress_current: number;
  progress_total: number;
  run_id: string;
  pipeline_generation: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_message: string | null;
  error_details: Record<string, unknown> | null;
  recovery_disposition: ProductionRecoveryDisposition;
  extraction_progress?: ExtractionProgress | null;
  extraction_rejections?: ExtractionRejections | null;
  reconciliation?: ProductionReconciliation | null;
  /**
   * Set when the run belongs to an edition production batch. Such a run is
   * only ever resumed through its batch, never restarted as a standalone one.
   */
  batch_id?: string | null;
  /**
   * Present exactly when the run is cancelled. Cancellation deletes nothing,
   * so the run can continue at its first stage without a live artifact.
   */
  resume_plan?: ProductionResumePlan | null;
  /** Parser recoveries worth showing, never blocking. */
  warnings: string[];
  stages: Record<string, StageStatus>;
}

export interface ProductionReconciliation {
  production_run_id: string;
  model_run_id: string;
  bridge_response_id: string | null;
  submission_state: string;
  phase: string;
  stage: ProductionStage;
  pipeline_generation: number;
  output_sha256: string | null;
  provenance: string | null;
  visible_available: boolean;
  batch_id: string | null;
}

export interface ProductionRecoveryPreview {
  production_run_id: string;
  model_run_id: string;
  stage: string;
  pipeline_generation: number;
  bridge_response_id: string | null;
  submission_state: string;
  phase: string;
  text: string;
  sha256: string;
  chars: number;
  metadata: Record<string, unknown>;
  visible_available: boolean;
}

export type ProductionReconciliationOutcome =
  "resumed" | "released" | "undecided";

export interface ProductionReconciliationProbeResult {
  outcome: ProductionReconciliationOutcome;
  bridge_status: string | null;
}

export function shouldPollProduction(
  status: ProductionStatus["status"] | undefined,
): boolean {
  return status === "queued" || status === "running";
}

export interface BatchItemDetail {
  position: number;
  subject_id: string;
  title: string;
  run_id: string;
  status: ProductionRunStatus;
  current_stage: ProductionStage;
  pipeline_generation: number;
  auto_recovery_count: number;
  error_code: string | null;
  error_message: string | null;
  extraction_progress?: ExtractionProgress | null;
  reconciliation?: ProductionReconciliation | null;
}

export interface ProductionSubject {
  subject_id: string;
  title: string;
  tlp: string;
  latest_run_id: string | null;
  latest_run_number: number | null;
  latest_status: ProductionRunStatus | null;
  latest_stage: ProductionStage | null;
  active_run_id: string | null;
  can_start: boolean;
  blocking_reason: string | null;
}

export interface ProductionRunSummary {
  run_id: string;
  edition_id: string;
  subject_id: string;
  run_number: number;
  status: ProductionRunStatus;
  current_stage: ProductionStage;
  pipeline_generation: number;
  research_date: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_message: string | null;
}

export interface ProductionBatch {
  batch_id: string;
  edition_id: string;
  status: ProductionBatchStatus;
  phase: ProductionBatchPhase;
  next_dispatch_at: string | null;
  items: number;
  completed: number;
  needs_review: number;
  failed: number;
  cancelled: number;
  item_details: BatchItemDetail[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export type BatchStatus = ProductionBatch;

export type ProductionBatchSummary = Omit<ProductionBatch, "item_details"> & {
  item_details?: BatchItemDetail[];
};

export interface ProductionBoard {
  edition_id: string;
  subjects: ProductionSubject[];
  active_batch: ProductionBatch | null;
  recent_batches: ProductionBatchSummary[];
}

export interface CancelProductionBatchResponse {
  action: string;
  batch_id: string;
  status: "cancelled";
  edition_state: EditionStatus;
  edition_version: number;
}

export interface ArtifactResponse {
  artifact_id: string;
  stage: string;
  version: number;
  status: "verified" | "stale" | "needs_review";
  reused?: boolean;
  reused_from_artifact_id?: string | null;
  reused_from_created_at?: string | null;
  metadata: Record<string, unknown>;
  /** Legacy rendered text; never present for the PUBLICATION stage. */
  rendered_content?: string | null;
  canonical_content:
    | PublicationDocumentV4
    | PublicationDocumentV5
    | ProductionExtractionV1
    | ProductionSynthesisV1
    | ExtractionDocumentV2
    | Record<string, unknown>
    | null;
}

export type ProductionExtractionTierV1 = "core" | "supporting" | "technical";
export type ProductionExtractionProfileV1 = "full" | "ioc_rules";
export type ProductionExtractionReuseStateV1 =
  "fresh" | "reused" | "content_duplicate";
export type ProductionExtractionOmissionReasonV1 =
  "reference_not_eligible" | "source_extraction_failed";
export const PRODUCTION_EXTRACTION_PROFILE_POLICY_VERSION =
  "production-reference-tier-v1" as const;

/** Every canonical element is proven by a local quote of its documents. */
export interface ProductionExtractionEvidenceV1 {
  context: string;
  evidence_quote: string;
  evidence_basis: string;
  source_document_ids: string[];
}

export interface ProductionExtractionFactV1 extends ProductionExtractionEvidenceV1 {
  category: string;
  value: string;
  attack_id: string | null;
}

export interface ProductionExtractionEventV1 extends ProductionExtractionEvidenceV1 {
  event_date: string | null;
  date_text: string | null;
  text: string;
}

export interface ProductionExtractionIndicatorV1 extends ProductionExtractionEvidenceV1 {
  value: string;
  artifact_type: string;
  indicator_status: "confirmed_ioc" | "contextual";
}

export interface ProductionExtractionRuleV1 extends ProductionExtractionEvidenceV1 {
  rule_type: string;
  name: string | null;
  body: string;
  sha256: string;
}

export interface ProductionSourceExtractionV1 {
  source_document_id: string;
  canonical_url: string;
  content_sha256: string;
  tier: ProductionExtractionTierV1;
  kind: "publication" | "technical_resource";
  role: string;
  profile: ProductionExtractionProfileV1;
  checkpoint_id: string | null;
  reuse_state: ProductionExtractionReuseStateV1;
  facts: ProductionExtractionFactV1[];
  events: ProductionExtractionEventV1[];
  indicators: ProductionExtractionIndicatorV1[];
  rules: ProductionExtractionRuleV1[];
  uncertainties: string[];
}

export interface ProductionExtractionOmissionV1 {
  canonical_url: string;
  tier: ProductionExtractionTierV1;
  collection_state: string;
  reason: ProductionExtractionOmissionReasonV1;
  error_code: string | null;
}

/** Mirror of the backend ``ProductionExtractionV1`` canonical payload. */
export interface ProductionExtractionV1 {
  schema_version: 1;
  subject_id: string;
  production_input_hash: string;
  references_corpus_hash: string;
  profile_policy_version: typeof PRODUCTION_EXTRACTION_PROFILE_POLICY_VERSION;
  sources: ProductionSourceExtractionV1[];
  omitted_sources: ProductionExtractionOmissionV1[];
  warnings: string[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
): boolean {
  const actual = Object.keys(value);
  return (
    actual.length === keys.length && actual.every((key) => keys.includes(key))
  );
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isUuid(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)
  );
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function isStringArray(value: unknown): value is string[] {
  return (
    Array.isArray(value) && value.every((item) => typeof item === "string")
  );
}

function isTier(value: unknown): value is ProductionExtractionTierV1 {
  return value === "core" || value === "supporting" || value === "technical";
}

const EVIDENCE_KEYS = [
  "context",
  "evidence_quote",
  "evidence_basis",
  "source_document_ids",
] as const;

function hasEvidence(value: Record<string, unknown>): boolean {
  return (
    typeof value.context === "string" &&
    isNonEmptyString(value.evidence_quote) &&
    isNonEmptyString(value.evidence_basis) &&
    Array.isArray(value.source_document_ids) &&
    value.source_document_ids.length > 0 &&
    value.source_document_ids.every(isUuid)
  );
}

function isFact(value: unknown): value is ProductionExtractionFactV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["category", "value", "attack_id", ...EVIDENCE_KEYS]) &&
    isNonEmptyString(value.category) &&
    isNonEmptyString(value.value) &&
    isNullableString(value.attack_id) &&
    hasEvidence(value)
  );
}

function isEvent(value: unknown): value is ProductionExtractionEventV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, [
      "event_date",
      "date_text",
      "text",
      ...EVIDENCE_KEYS,
    ]) &&
    (value.event_date === null ||
      (typeof value.event_date === "string" &&
        /^\d{4}-\d{2}-\d{2}$/.test(value.event_date))) &&
    isNullableString(value.date_text) &&
    isNonEmptyString(value.text) &&
    hasEvidence(value)
  );
}

function isIndicator(value: unknown): value is ProductionExtractionIndicatorV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, [
      "value",
      "artifact_type",
      "indicator_status",
      ...EVIDENCE_KEYS,
    ]) &&
    isNonEmptyString(value.value) &&
    isNonEmptyString(value.artifact_type) &&
    (value.indicator_status === "confirmed_ioc" ||
      value.indicator_status === "contextual") &&
    hasEvidence(value)
  );
}

function isRule(value: unknown): value is ProductionExtractionRuleV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, [
      "rule_type",
      "name",
      "body",
      "sha256",
      ...EVIDENCE_KEYS,
    ]) &&
    isNonEmptyString(value.rule_type) &&
    isNullableString(value.name) &&
    isNonEmptyString(value.body) &&
    isSha256(value.sha256) &&
    hasEvidence(value)
  );
}

function isSource(value: unknown): value is ProductionSourceExtractionV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, [
      "source_document_id",
      "canonical_url",
      "content_sha256",
      "tier",
      "kind",
      "role",
      "profile",
      "checkpoint_id",
      "reuse_state",
      "facts",
      "events",
      "indicators",
      "rules",
      "uncertainties",
    ]) &&
    isUuid(value.source_document_id) &&
    isNonEmptyString(value.canonical_url) &&
    isSha256(value.content_sha256) &&
    isTier(value.tier) &&
    (value.kind === "publication" || value.kind === "technical_resource") &&
    isNonEmptyString(value.role) &&
    // The frozen REFERENCES tier alone decides the profile.
    value.profile === (value.tier === "core" ? "full" : "ioc_rules") &&
    (value.checkpoint_id === null || isUuid(value.checkpoint_id)) &&
    (value.reuse_state === "fresh" ||
      value.reuse_state === "reused" ||
      value.reuse_state === "content_duplicate") &&
    Array.isArray(value.facts) &&
    value.facts.every(isFact) &&
    Array.isArray(value.events) &&
    value.events.every(isEvent) &&
    Array.isArray(value.indicators) &&
    value.indicators.every(isIndicator) &&
    Array.isArray(value.rules) &&
    value.rules.every(isRule) &&
    isStringArray(value.uncertainties)
  );
}

function isOmission(value: unknown): value is ProductionExtractionOmissionV1 {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "canonical_url",
      "tier",
      "collection_state",
      "reason",
      "error_code",
    ]) ||
    !isNonEmptyString(value.canonical_url) ||
    !isTier(value.tier) ||
    !isNonEmptyString(value.collection_state)
  ) {
    return false;
  }
  if (value.reason === "reference_not_eligible") {
    return value.error_code === null;
  }
  return (
    value.reason === "source_extraction_failed" &&
    isNonEmptyString(value.error_code)
  );
}

export function isProductionExtractionV1(
  value: unknown,
): value is ProductionExtractionV1 {
  if (
    !isRecord(value) ||
    !hasExactKeys(value, [
      "schema_version",
      "subject_id",
      "production_input_hash",
      "references_corpus_hash",
      "profile_policy_version",
      "sources",
      "omitted_sources",
      "warnings",
    ]) ||
    value.schema_version !== 1 ||
    !isUuid(value.subject_id) ||
    !isSha256(value.production_input_hash) ||
    !isSha256(value.references_corpus_hash) ||
    value.profile_policy_version !==
      PRODUCTION_EXTRACTION_PROFILE_POLICY_VERSION ||
    !Array.isArray(value.sources) ||
    !value.sources.every(isSource) ||
    !Array.isArray(value.omitted_sources) ||
    !value.omitted_sources.every(isOmission) ||
    !isStringArray(value.warnings)
  ) {
    return false;
  }
  const documentIds = value.sources.map((source) => source.source_document_id);
  return new Set(documentIds).size === documentIds.length;
}

export type ProductionSynthesisEvidenceKindV1 =
  "fact" | "event" | "indicator" | "rule";

/** Structural identity of a synthesis section, never a style name. */
export type ProductionSynthesisSectionKindV1 =
  | "overview"
  | "campaign"
  | "infection_chain"
  | "technical"
  | "victimology"
  | "infrastructure"
  | "detection"
  | "impact"
  | "other";

export const PRODUCTION_SYNTHESIS_POLICY_VERSION =
  "production-synthesis-v1" as const;

/**
 * Canonical identity of one piece of evidence. The ``evidence_key`` is an
 * internal content hash; it is never a temporary prompt handle.
 */
export interface ProductionSynthesisEvidenceRefV1 {
  source_document_id: string;
  kind: ProductionSynthesisEvidenceKindV1;
  evidence_key: string;
}

export interface ProductionSynthesisParagraphV1 {
  text: string;
  evidence_refs: ProductionSynthesisEvidenceRefV1[];
}

export interface ProductionSynthesisSectionV1 {
  kind: ProductionSynthesisSectionKindV1;
  heading: string;
  paragraphs: ProductionSynthesisParagraphV1[];
}

export interface ProductionSynthesisTimelineEntryV1 {
  event_date: string | null;
  date_text: string | null;
  text: string;
  evidence_refs: ProductionSynthesisEvidenceRefV1[];
}

export interface ProductionSynthesisUncertaintyV1 {
  text: string;
  source_document_ids: string[];
}

/** Mirror of the backend ``ProductionSynthesisV1`` canonical payload. */
export interface ProductionSynthesisV1 {
  schema_version: 1;
  subject_id: string;
  production_input_hash: string;
  extraction_hash: string;
  publication_language: string;
  synthesis_policy_version: typeof PRODUCTION_SYNTHESIS_POLICY_VERSION;
  title: string;
  lead: ProductionSynthesisParagraphV1[];
  sections: ProductionSynthesisSectionV1[];
  timeline: ProductionSynthesisTimelineEntryV1[];
  uncertainties: ProductionSynthesisUncertaintyV1[];
  warnings: string[];
}

const SYNTHESIS_EVIDENCE_KINDS = new Set<string>([
  "fact",
  "event",
  "indicator",
  "rule",
]);

const SYNTHESIS_SECTION_KINDS = new Set<string>([
  "overview",
  "campaign",
  "infection_chain",
  "technical",
  "victimology",
  "infrastructure",
  "detection",
  "impact",
  "other",
]);

function isIsoDate(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    !Number.isNaN(Date.parse(`${value}T00:00:00Z`))
  );
}

function isNonEmptyStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => isNonEmptyString(item));
}

function isSynthesisEvidenceRef(
  value: unknown,
): value is ProductionSynthesisEvidenceRefV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["source_document_id", "kind", "evidence_key"]) &&
    isUuid(value.source_document_id) &&
    typeof value.kind === "string" &&
    SYNTHESIS_EVIDENCE_KINDS.has(value.kind) &&
    isSha256(value.evidence_key)
  );
}

function isSynthesisEvidenceRefs(
  value: unknown,
): value is ProductionSynthesisEvidenceRefV1[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every(isSynthesisEvidenceRef)
  );
}

function isSynthesisParagraph(
  value: unknown,
): value is ProductionSynthesisParagraphV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["text", "evidence_refs"]) &&
    isNonEmptyString(value.text) &&
    isSynthesisEvidenceRefs(value.evidence_refs)
  );
}

function isSynthesisSection(
  value: unknown,
): value is ProductionSynthesisSectionV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["kind", "heading", "paragraphs"]) &&
    typeof value.kind === "string" &&
    SYNTHESIS_SECTION_KINDS.has(value.kind) &&
    typeof value.heading === "string" &&
    Array.isArray(value.paragraphs) &&
    value.paragraphs.length > 0 &&
    value.paragraphs.every(isSynthesisParagraph)
  );
}

function isSynthesisTimelineEntry(
  value: unknown,
): value is ProductionSynthesisTimelineEntryV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["event_date", "date_text", "text", "evidence_refs"]) &&
    (value.event_date === null || isIsoDate(value.event_date)) &&
    (value.date_text === null || isNonEmptyString(value.date_text)) &&
    isNonEmptyString(value.text) &&
    isSynthesisEvidenceRefs(value.evidence_refs)
  );
}

function isSynthesisUncertainty(
  value: unknown,
): value is ProductionSynthesisUncertaintyV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, ["text", "source_document_ids"]) &&
    isNonEmptyString(value.text) &&
    Array.isArray(value.source_document_ids) &&
    value.source_document_ids.length > 0 &&
    value.source_document_ids.every(isUuid)
  );
}

/** Decode only exact canonical V1 payloads; unknown fields are refused. */
export function isProductionSynthesisV1(
  value: unknown,
): value is ProductionSynthesisV1 {
  return (
    isRecord(value) &&
    hasExactKeys(value, [
      "schema_version",
      "subject_id",
      "production_input_hash",
      "extraction_hash",
      "publication_language",
      "synthesis_policy_version",
      "title",
      "lead",
      "sections",
      "timeline",
      "uncertainties",
      "warnings",
    ]) &&
    value.schema_version === 1 &&
    isUuid(value.subject_id) &&
    isSha256(value.production_input_hash) &&
    isSha256(value.extraction_hash) &&
    isNonEmptyString(value.publication_language) &&
    value.synthesis_policy_version === PRODUCTION_SYNTHESIS_POLICY_VERSION &&
    isNonEmptyString(value.title) &&
    Array.isArray(value.lead) &&
    value.lead.every(isSynthesisParagraph) &&
    Array.isArray(value.sections) &&
    value.sections.every(isSynthesisSection) &&
    Array.isArray(value.timeline) &&
    value.timeline.every(isSynthesisTimelineEntry) &&
    Array.isArray(value.uncertainties) &&
    value.uncertainties.every(isSynthesisUncertainty) &&
    isNonEmptyStringArray(value.warnings)
  );
}

export interface ExtractionItemV2 {
  id: string;
  category: string;
  value: string;
  context: string;
  artifact_type: string | null;
  semantic_type: string;
  indicator_status:
    "confirmed_ioc" | "contextual" | "excluded" | "not_applicable";
  provenance: string;
  display_policy: "ioc_section" | "body_only" | "both" | "hidden";
  normalized_value: string | null;
  evidence_quote: string | null;
  source_ids: string[];
}

export interface ExtractionDocumentV2 {
  schema_version: "2";
  parser_version: string;
  items: ExtractionItemV2[];
  uncertainties: string[];
}

export interface ProductionStateRepairDecision {
  repair_key: string;
  decision_id: string | null;
  issue_kind: string;
  action: string;
  actor_id: string;
  decided_at: string;
  reason: string | null;
}

export interface ProductionStateRepair {
  projection_version: string;
  base_extraction_artifact_id: string;
  actor_id: string | null;
  included_repair_keys: string[];
  excluded_repair_keys: string[];
  unresolved_repair_keys: string[];
  decisions: ProductionStateRepairDecision[];
  materialization: Record<string, unknown> | null;
}

export interface ProductionStateSnapshotV5 {
  format: "autowork.production-state";
  schema_version: 5;
  exported_at: string;
  origin: {
    subject_title: string;
    subject_id: string;
    production_run_id: string;
    research_date: string;
    discovery_snapshot_id: string;
    discovery_snapshot_version: number;
  };
  artifacts: {
    references: {
      input_hash: string;
      canonical_content: Record<string, unknown>;
    };
    extraction: {
      input_hash: string;
      canonical_content: Record<string, unknown>;
    };
    synthesis: {
      input_hash: string;
      canonical_content: Record<string, unknown>;
    };
    editorial_enrichment: {
      input_hash: string;
      canonical_content: Record<string, unknown>;
    };
  };
  repair?: ProductionStateRepair | null;
  content_sha256: string;
}

export type ProductionStateSnapshot = ProductionStateSnapshotV5;

export interface ProductionStateImportResult {
  run_id: string;
  status: "needs_review";
  current_stage: "relevance_projection";
  imported_stages: [
    "references",
    "extraction",
    "synthesis",
    "editorial_enrichment",
  ];
  schema_version: 5;
  content_sha256: string;
}

export interface PublicationEvidenceRefV1 {
  source_document_id: string;
  kind: "fact" | "event" | "indicator" | "rule";
  evidence_key: string;
}

export interface PublicationTableColumnV1 {
  key: string;
  label: string;
}

export interface PublicationTableRowV1 {
  cells: string[];
  evidence_refs: PublicationEvidenceRefV1[];
}

export interface PublicationPlacementV1 {
  kind: string;
  section_index: number | null;
}

export interface PublicationTableV1 {
  key: string;
  kind: string;
  title: string;
  caption: string | null;
  columns: PublicationTableColumnV1[];
  rows: PublicationTableRowV1[];
  placement: PublicationPlacementV1;
}

export interface PublicationDiagramNodeV1 {
  node_id: string;
  label: string;
  evidence_refs: PublicationEvidenceRefV1[];
}

export interface PublicationDiagramEdgeV1 {
  source_node_id: string;
  target_node_id: string;
  label: string | null;
  evidence_refs: PublicationEvidenceRefV1[];
}

export interface PublicationDiagramGroupV1 {
  group_id: string;
  label: string;
  node_ids: string[];
}

export interface PublicationDiagramV1 {
  key: string;
  kind: string;
  title: string;
  caption: string | null;
  direction: string;
  nodes: PublicationDiagramNodeV1[];
  edges: PublicationDiagramEdgeV1[];
  groups: PublicationDiagramGroupV1[];
  placement: PublicationPlacementV1;
  asset_id: string;
}

export interface PublicationSourceFigureLocatorV1 {
  page: number | null;
  section: string | null;
  figure_label: string | null;
  original_asset_url: string | null;
}

export interface PublicationSourceFigureV1 {
  key: string;
  asset_id: string;
  sha256: string;
  mime_type: string;
  byte_size: number;
  source_document_id: string;
  source_url: string;
  caption: string;
  provenance: string;
  locator: PublicationSourceFigureLocatorV1;
  placement: PublicationPlacementV1;
}

export interface PublicationDocumentV4 {
  schema_version: "4";
  subject_id: string;
  publication_language: string;
  title: string;
  lead: Array<{ text: string; evidence_refs: PublicationEvidenceRefV1[] }>;
  sections: Array<{
    kind: string;
    heading: string;
    paragraphs: Array<{
      text: string;
      evidence_refs: PublicationEvidenceRefV1[];
    }>;
  }>;
  timeline: Array<{
    event_date: string | null;
    date_text: string | null;
    text: string;
    evidence_refs: PublicationEvidenceRefV1[];
  }>;
  indicators: Array<{
    artifact_type: string;
    indicators: Array<{
      value: string;
      normalized_value: string;
      artifact_type: string;
      source_document_ids: string[];
    }>;
  }>;
  sources: Array<{
    source_document_id: string;
    canonical_url: string;
    title: string | null;
    publisher: string | null;
    published_at: string | null;
    tier: string;
    kind: string;
    role: string;
  }>;
  uncertainties: Array<{ text: string; source_document_ids: string[] }>;
  tables: PublicationTableV1[];
  diagrams: PublicationDiagramV1[];
  figures: PublicationSourceFigureV1[];
}

export type PublicationSemanticRoleV1 =
  | "text"
  | "actor"
  | "campaign"
  | "malware"
  | "tool"
  | "product"
  | "english_term"
  | "technical"
  | "technical_literal"
  | "ioc"
  | "path"
  | "command"
  | "protocol_field"
  | "source"
  | "proof";

export interface PublicationSemanticSpanV1 {
  role: PublicationSemanticRoleV1;
  text: string;
}

export interface PublicationSemanticParagraphV1 {
  anchor: string;
  spans: PublicationSemanticSpanV1[];
}

export interface PublicationSemanticTextV1 {
  schema_version: "1";
  policy_version: string;
  paragraphs: PublicationSemanticParagraphV1[];
}

export interface PublicationDocumentV5 extends Omit<
  PublicationDocumentV4,
  "schema_version"
> {
  schema_version: "5";
  rich_text: PublicationSemanticTextV1;
}

export type PublicationDocument = PublicationDocumentV4 | PublicationDocumentV5;

export type PublicationPreviewStatus =
  "IN_PROGRESS" | "READY" | "FAILED" | "STALE";

export interface PublicationArtifactPreview {
  status: PublicationPreviewStatus;
  artifact_id: string;
  artifact_version: number;
  artifact_input_hash: string;
  current_artifact_id: string;
  current_artifact_version: number;
  render_id: string | null;
  render_identity: string | null;
  render_disposition: "ACCEPTED_VERSION" | "EXPLICIT_RENDER";
  published_edition_version: number | null;
  error_code: string | null;
  error_message: string | null;
  pdf_url: string | null;
}

export async function restartProductionWithNewSources(
  subjectId: string,
): Promise<{ run_id: string; replaced_run_id: string }> {
  return request(
    `/api/production/subjects/${encodeURIComponent(subjectId)}/production/restart-with-new-sources`,
    { method: "POST" },
  );
}

/**
 * Get production status for a subject.
 *
 * Returns null when no production has been started yet — that is a normal
 * state, not an error, and it is what makes the UI offer a start button.
 */
export async function getSubjectProduction(
  subjectId: string,
): Promise<ProductionStatus | null> {
  return requestOrNull(`/api/subjects/${subjectId}/production`);
}

export async function getSubjectProductionRuns(
  subjectId: string,
): Promise<ProductionRunSummary[]> {
  return request(`/api/subjects/${subjectId}/production/runs`);
}

export async function exportProductionState(
  subjectId: string,
): Promise<ProductionStateSnapshotV5> {
  return request(`/api/subjects/${subjectId}/production/state/export`);
}

export async function importProductionState(
  subjectId: string,
  snapshot: ProductionStateSnapshot,
): Promise<ProductionStateImportResult> {
  return request(`/api/subjects/${subjectId}/production/state/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(snapshot),
  });
}

/** Recompute one stage in place, invalidating its downstream artifacts. */
export async function retryProductionStage(
  subjectId: string,
  stage: ProductionStage,
): Promise<{
  run_id: string;
  status: string;
  job_id: string | null;
  pipeline_generation: number;
}> {
  return request(`/api/subjects/${subjectId}/production/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage }),
  });
}

export async function resumeProduction(subjectId: string): Promise<{
  run_id: string;
  status: string;
  job_id: string | null;
  pipeline_generation: number;
  resume_plan: ProductionResumePlan;
}> {
  return request(`/api/subjects/${subjectId}/production/resume`, {
    method: "POST",
  });
}

export async function resumeProductionRun(runId: string): Promise<{
  run_id: string;
  status: string;
  job_id: string | null;
  pipeline_generation: number;
  resume_plan: ProductionResumePlan;
}> {
  return request(`/api/production/runs/${runId}/resume`, { method: "POST" });
}

export async function previewProductionReconciliationVisible(
  runId: string,
): Promise<ProductionRecoveryPreview> {
  return request(
    `/api/production/runs/${runId}/reconciliation/visible/preview`,
    {
      method: "POST",
    },
  );
}

export async function probeProductionReconciliation(
  runId: string,
): Promise<ProductionReconciliationProbeResult> {
  return request(`/api/production/runs/${runId}/reconciliation/probe`, {
    method: "POST",
  });
}

export async function declareProductionReconciliationLost(
  runId: string,
  reason: string,
): Promise<{ outcome: "released"; declared_lost: true }> {
  return request(`/api/production/runs/${runId}/reconciliation/declare-lost`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true, reason }),
  });
}

export async function adoptProductionReconciliationVisible(
  runId: string,
  expectedSha256: string,
): Promise<Record<string, unknown>> {
  return request(`/api/production/runs/${runId}/reconciliation/visible/adopt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_sha256: expectedSha256 }),
  });
}

export async function previewProductionReconciliationManual(
  runId: string,
  markdown: string,
): Promise<ProductionRecoveryPreview> {
  return request(
    `/api/production/runs/${runId}/reconciliation/manual/preview`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown }),
    },
  );
}

export async function adoptProductionReconciliationManual(
  runId: string,
  markdown: string,
  expectedSha256: string,
): Promise<Record<string, unknown>> {
  return request(`/api/production/runs/${runId}/reconciliation/manual/adopt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ markdown, expected_sha256: expectedSha256 }),
  });
}

/** Block future cross-run reuse from this costly stage onward. */
export async function invalidateProductionReuse(
  subjectId: string,
  fromStage: "references" | "extraction" | "synthesis",
): Promise<{ action: string; from_stage: string; occurred_at: string }> {
  return request(`/api/subjects/${subjectId}/production/reuse/invalidate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ from_stage: fromStage }),
  });
}

export async function cancelProductionBatch(
  editionId: string,
  batchId: string,
): Promise<CancelProductionBatchResponse> {
  return request(`/api/editions/${editionId}/production/${batchId}/cancel`, {
    method: "POST",
  });
}

export async function getReferencesArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(`/api/subjects/${subjectId}/production/artifacts/references`);
}

export async function getExtractionArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(`/api/subjects/${subjectId}/production/artifacts/extraction`);
}

export async function getRelevanceProjectionArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(
    `/api/subjects/${subjectId}/production/artifacts/relevance_projection`,
  );
}

export async function getSynthesisArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(`/api/subjects/${subjectId}/production/artifacts/synthesis`);
}

export async function getEditorialEnrichmentArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(
    `/api/subjects/${subjectId}/production/artifacts/editorial_enrichment`,
  );
}

export async function getPublicationArtifact(
  subjectId: string,
): Promise<ArtifactResponse> {
  return request(`/api/subjects/${subjectId}/production/artifacts/publication`);
}

export async function getPublicationArtifactPreview(
  subjectId: string,
  artifactId: string,
): Promise<PublicationArtifactPreview> {
  const params = new URLSearchParams({ artifact_id: artifactId });
  return request(
    `/api/subjects/${subjectId}/publication/preview?${params.toString()}`,
  );
}

export async function getPublicationPreviewPdf(
  preview: PublicationArtifactPreview,
): Promise<Blob> {
  if (preview.status !== "READY" || preview.pdf_url === null) {
    throw new Error("Le PDF de cet artifact n’est pas disponible.");
  }
  const response = await fetch(preview.pdf_url);
  if (!response.ok) throw await apiError(response);
  if (!response.headers.get("content-type")?.includes("application/pdf")) {
    throw new Error("La réponse de l’aperçu n’est pas un PDF.");
  }
  return response.blob();
}

// Edition production API

/**
 * Start batch production for an edition, for exactly the given subjects.
 *
 * `subjectIds` must be the operator's explicit production-batch selection —
 * a subset of the already-materialized Subjects, in selection board order.
 * Selection eligibility (a board item in state `selected` with a
 * `subject_id`) is a separate notion from this batch selection: subjects
 * left unchecked are never sent here and keep their Selection decision.
 * An empty selection is refused client-side rather than silently falling
 * back to "every eligible subject" — the caller must ask the operator to
 * choose at least one subject.
 */
export async function startProductionBatch(
  editionId: string,
  subjectIds: readonly string[],
  idempotencyKey: string,
): Promise<BatchStatus> {
  if (subjectIds.length === 0) {
    throw new Error(
      "startProductionBatch requires at least one selected subject",
    );
  }
  return request(`/api/editions/${editionId}/production/batches`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({ subject_ids: subjectIds }),
  });
}

/**
 * Get the edition production board.
 *
 * Existing editions always return a board, including an empty board when no
 * subject or batch exists yet.
 */
export async function getEditionProduction(
  editionId: string,
): Promise<ProductionBoard> {
  return request(`/api/editions/${editionId}/production`);
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (response.ok) return (await response.json()) as T;
  throw await apiError(response);
}

/**
 * Like `request`, but treats 404 as "nothing here yet" rather than a failure.
 */
async function requestOrNull<T>(
  url: string,
  init?: RequestInit,
): Promise<T | null> {
  const response = await fetch(url, init);
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
      : detail?.message ||
        "La production n\u2019a pas pu \u00eatre effectu\u00e9e.";
  return new ApiError(
    message,
    typeof detail === "object" && detail?.code
      ? detail.code
      : "production_error",
    response.status,
  );
}
