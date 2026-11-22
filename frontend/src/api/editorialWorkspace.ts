import { ApiError } from "./editions";
import type { EditorialReadOnlyReason } from "./editorialReadOnly";

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
export type EditorialImpactStatus =
  "none" | "pending" | "running" | "superseded" | "up_to_date" | "failed";
export type EditorialPdfStatus = "CURRENT" | "STALE";
export type EditorialPdfPreviewStatus =
  "READY" | "STALE" | "GENERATING" | "FAILED";

export type EditorialFigureStatus =
  | "available"
  | "already_integrated"
  | "not_selected"
  | "ineligible"
  | "file_unavailable"
  | "unverified";

export interface EditorialFigureSource {
  source_id: string;
  title: string | null;
  url: string | null;
}

export interface EditorialFigureCatalogItem {
  figure_id: string;
  original_name: string;
  original_caption: string | null;
  alt_text: string | null;
  source: EditorialFigureSource;
  page: number | null;
  section: string | null;
  context_before: string | null;
  context_after: string | null;
  width: number | null;
  height: number | null;
  mime_type: string | null;
  status: EditorialFigureStatus;
  selectable: boolean;
  exclusion_reason: string | null;
  already_used: boolean;
}

export interface EditorialFigureCatalogPage {
  items: EditorialFigureCatalogItem[];
  sources: EditorialFigureSource[];
  total: number;
  available_count: number;
  offset: number;
  limit: number;
}

export interface EditorialFigurePreview extends EditorialFigureCatalogItem {
  preview_image_url: string;
}

export interface EditorialFigureCatalogFilters {
  status?: EditorialFigureStatus;
  source?: string;
  q?: string;
}

export interface EditorialWorkspaceRevisionTarget {
  block_id: string;
  legacy_target_id: string | null;
}

export interface EditorialWorkspaceTableRow {
  cells: string[];
  evidence_refs: EditorialEvidenceReference[];
}

export interface EditorialWorkspaceTable {
  key: string;
  kind: EditorialEnrichmentTableKind;
  title: string;
  caption: string | null;
  columns: EditorialTableColumn[];
  rows: EditorialWorkspaceTableRow[];
  evidence_refs: EditorialEvidenceReference[];
  anchor_block_id: string | null;
  placement:
    | "after_paragraph"
    | "after_lead"
    | "after_timeline"
    | "after_section"
    | "end";
}

export interface EditorialWorkspaceBarChartPoint {
  label: string;
  value: string;
  evidence_refs: EditorialEvidenceReference[];
}

export interface EditorialWorkspaceBarChart {
  key: string;
  kind: "bar";
  title: string;
  caption: string | null;
  legend: string[];
  category_axis_label: string;
  value_axis_label: string;
  unit: string;
  points: EditorialWorkspaceBarChartPoint[];
  anchor_block_id: string;
}

export interface EditorialWorkspaceFigure {
  key: string;
  catalog_figure_id: string | null;
  original_caption?: string | null;
  caption: string;
  source_url: string;
  source: string | null;
  provenance: string;
  sha256: string;
  mime_type: string;
  byte_size: number;
  width: number | null;
  height: number | null;
  status: "proposed" | "included" | "excluded";
  order: number | null;
  anchor_block_id: string | null;
  placement:
    | "after_paragraph"
    | "after_lead"
    | "after_timeline"
    | "after_section"
    | "end";
  page: number | null;
  section: string | null;
}

export type EditorialEnrichmentElementKind =
  "table" | "diagram" | "chart" | "figure";

export interface EditorialWorkspaceDiagramNode {
  node_id: string;
  label: string;
  role: string | null;
}

export interface EditorialWorkspaceDiagramEdge {
  source_node_id: string;
  target_node_id: string;
  label: string | null;
}

export interface EditorialWorkspaceDiagramGroup {
  group_id: string;
  label: string;
  node_ids: string[];
}

export interface EditorialWorkspaceDiagram {
  key: string;
  kind: string | null;
  title: string;
  caption: string | null;
  direction: string;
  profile: string | null;
  nodes: EditorialWorkspaceDiagramNode[];
  edges: EditorialWorkspaceDiagramEdge[];
  groups: EditorialWorkspaceDiagramGroup[];
  anchor_block_id: string | null;
  placement:
    | "after_paragraph"
    | "after_lead"
    | "after_timeline"
    | "after_section"
    | "end";
}

export interface EditorialWorkspaceBlock {
  block_id: string;
  kind: EditorialBlockKind;
  parent_block_id: string | null;
  position: number;
  text: string | null;
  evidence_refs: Record<string, unknown>[];
  table?: EditorialWorkspaceTable | null;
  chart: EditorialWorkspaceBarChart | null;
  figure?: EditorialWorkspaceFigure | null;
  diagram?: EditorialWorkspaceDiagram | null;
  revision_target: EditorialWorkspaceRevisionTarget;
  before_hash: string | null;
  allowed_operations: string[];
  read_only_reason?: EditorialReadOnlyReason | null;
  revision_status: EditorialRevisionStatus;
  element_kind?: EditorialEnrichmentElementKind | null;
  element_key?: string | null;
  order?: number | null;
  caption?: string | null;
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
  can_regenerate_pdf: boolean;
  read_only_reason?: EditorialReadOnlyReason | null;
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
  preview_status: EditorialPdfPreviewStatus;
  impact_status: EditorialImpactStatus;
  pdf_status: EditorialPdfStatus;
  pdf_artifact_id: string | null;
  pdf_artifact_version: number | null;
  pdf_artifact_input_hash: string | null;
  pdf_artifact_canonical_sha256: string | null;
  pdf_render_id: string | null;
  pdf_render_identity: string | null;
  pdf_error_code: string | null;
  pdf_error_message: string | null;
  pdf_url: string | null;
  snapshot_id: string;
}

export interface EditorialPdfStatusView {
  subject_id: string;
  status: EditorialPdfPreviewStatus;
  artifact_id: string | null;
  artifact_version: number | null;
  artifact_input_hash: string | null;
  artifact_canonical_sha256: string | null;
  render_id: string | null;
  render_identity: string | null;
  error_code: string | null;
  error_message: string | null;
  pdf_url: string | null;
}

export interface EditorialHistoryItem {
  proposal_id: string;
  subject_id: string;
  occurrence_id: string;
  state: EditorialProposalState;
  origin: "manual_patch" | "bridge_draft";
  operation: EditorialProposalOperation;
  operation_label: string;
  actor_id: string;
  actor_display: string;
  created_at: string;
  updated_at: string;
  canonical_result_artifact_id: string | null;
  canonical_result_version: number | null;
  canonical_result_stage: string | null;
  thread_id: string | null;
  thread_url: string | null;
}

export interface EditorialHistoryPage {
  items: EditorialHistoryItem[];
  next_cursor: string | null;
  limit: number;
}

export interface EditorialCompositionStatusView {
  edition_id: string;
  subject_id: string;
  status: "current" | "stale" | "not_pinned";
  message: string;
  composition_id: string | null;
  composition_version: number | null;
  pinned_artifact_id: string | null;
  pinned_artifact_version: number | null;
  current_artifact_id: string | null;
  current_artifact_version: number | null;
  current_version_available: boolean;
  composition_url: string;
}

export interface EditorialServerDraft {
  id: string;
  subject_id: string;
  production_run_id: string;
  occurrence_id: string;
  base_snapshot_id: string;
  base_artifact_id: string;
  base_artifact_version: number;
  base_canonical_sha256: string;
  text_sha256: string;
  version: number;
  text: string;
  is_stale: boolean;
  created_at: string;
  updated_at: string;
  expires_at: string;
}

export interface ResolvedEditorialWorkspaceBlock {
  snapshot_id: string;
  block_id: string;
  path: string;
  before_hash: string;
  operation: string;
}

export type EditorialThreadBridgeStatus =
  | "ready"
  | "waiting_for_capacity"
  | "generating"
  | "reply_available"
  | "refused"
  | "session_lost"
  | "reconciliation_required";

export type EditorialThreadEventKind =
  | "user_message"
  | "bridge_reply"
  | "proposal"
  | "accepted"
  | "rejected"
  | "refused"
  | "reconciliation"
  | "generation";

export type EditorialThreadReconciliationOutcome =
  "reconciled_reply" | "not_submitted" | "still_unknown" | "session_lost";

export type EditorialThreadSendGeneration =
  | "queued"
  | "waiting_for_capacity"
  | "running"
  | "reconciliation_required"
  | "complete"
  | "failed"
  | "cancelled";

export interface EditorialThreadSendResult {
  kind: "accepted" | "already_known";
  thread_id: string;
  entry_id: string;
  generation: EditorialThreadSendGeneration;
}

export interface EditorialThreadBinding {
  subject_id: string;
  production_run_id: string;
  occurrence_id: string;
  artifact_id: string;
  artifact_version: number;
  canonical_sha256: string;
}

export interface EditorialThreadTimelineEvent {
  id: string;
  sequence: number;
  kind: EditorialThreadEventKind;
  actor_id: string | null;
  proposal_id: string | null;
  created_at: string;
  body: string | null;
  reconciliation_outcome?: EditorialThreadReconciliationOutcome | null;
}

interface EditorialThreadReadBase {
  current: EditorialThreadBinding;
  bridge_status: EditorialThreadBridgeStatus;
  event_sequence: number;
  timeline: EditorialThreadTimelineEvent[];
  etag: string;
}

export interface EditorialThreadUnopenedRead extends EditorialThreadReadBase {
  kind: "unopened";
  binding: EditorialThreadBinding;
}

export interface EditorialThreadOpenRead extends EditorialThreadReadBase {
  kind: "open";
  thread_id: string;
  origin: EditorialThreadBinding;
  binding_state: "valid";
  previous_sessions: number[];
}

export interface EditorialThreadStaleRead extends EditorialThreadReadBase {
  kind: "stale";
  thread_id: string | null;
  origin: EditorialThreadBinding | null;
  binding_state: "stale" | "ambiguous";
}

export type EditorialThreadRead =
  | EditorialThreadUnopenedRead
  | EditorialThreadOpenRead
  | EditorialThreadStaleRead;

export interface EditorialThreadReconciliationResult {
  thread_id: string;
  outcome: EditorialThreadReconciliationOutcome;
  message: string;
}

export interface EditorialThreadSessionStartResult {
  kind: "session_started";
  thread_id: string;
  session_number: number;
}

export type EditorialProposalState =
  | "preparing"
  | "proposed"
  | "refused"
  | "needs_review"
  | "accepted"
  | "rejected"
  | "stale";

export type EditorialProposalRefusalCode = "revision_not_applicable";

export type EditorialProposalOperation =
  | "rewrite_title"
  | "rewrite_lead"
  | "rewrite_paragraph"
  | "insert_paragraph"
  | "delete_paragraph"
  | "rewrite_section"
  | "insert_section"
  | "delete_section"
  | "merge_sections"
  | "split_section"
  | "rename_section"
  | "create_table"
  | "create_bar_chart"
  | "rewrite_table"
  | "delete_table"
  | "create_diagram"
  | "rewrite_diagram"
  | "delete_diagram"
  | "create_chart"
  | "rewrite_chart"
  | "delete_chart"
  | "create_source_figure"
  | "rewrite_source_figure"
  | "replace_source_figure"
  | "delete_source_figure"
  | "update_visual_caption"
  | "move_enrichment_element"
  | "reorder_enrichment_elements";

export interface EditorialProposalPatch {
  schema_version: "editorial-patch-v1";
  operation: EditorialProposalOperation;
  target: Record<string, unknown>;
  target_id: string;
  occurrence_id: string;
  before_hash: string;
  rationale: string;
  after?: string;
  enrichment_patch?: EditorialEnrichmentProposalPatch;
  evidence_refs?: EditorialEvidenceReference[];
}

export type EditorialEnrichmentTableKind =
  | "commands"
  | "dropped_files"
  | "tools"
  | "techniques"
  | "infrastructure"
  | "timeline"
  | "configuration"
  | "custom";

export interface EditorialEvidenceReference {
  source_document_id: string;
  kind:
    "fact" | "event" | "source_metadata" | "indicator" | "rule" | "uncertainty";
  evidence_key: string;
}

export interface EditorialTableColumn {
  key: string;
  label: string;
}

export interface EditorialTableRow {
  cells: string[];
  evidence_handles: string[];
}

export interface EditorialTablePurpose {
  question: string;
  available_data: string;
  comprehension_gain: string;
  scope: string;
  evidence_handles: string[];
  knowledge_limits: string;
  placement_reason: string;
}

export interface EditorialTableProposal {
  key: string;
  kind: EditorialEnrichmentTableKind;
  title: string;
  caption: string | null;
  columns: EditorialTableColumn[];
  rows: EditorialTableRow[];
  placement: {
    kind:
      | "after_paragraph"
      | "after_lead"
      | "after_timeline"
      | "after_section"
      | "end";
    section_index?: number | null;
    anchor_block_id?: string | null;
  };
  purpose: EditorialTablePurpose;
}

export interface EditorialEnrichmentProposalPatch {
  tables?: EditorialTableProposal[];
  charts?: unknown[];
  diagrams?: unknown[];
  annotations?: unknown[];
  figures?: unknown[];
  manual_source_figures?: EditorialManualSourceFigurePatchReference[];
  resource_needs?: unknown[];
  [key: string]: unknown;
}

/** Persisted, non-canonical payload of one typed visual revision proposal. */
export type EditorialVisualRevisionPatch =
  | {
      kind: "update_visual_caption";
      element_kind: EditorialEnrichmentElementKind;
      element_key: string;
      caption: string | null;
    }
  | {
      kind: "move_enrichment_element";
      element_kind: EditorialEnrichmentElementKind;
      element_key: string;
      anchor_block_id: string;
    }
  | {
      kind: "reorder_enrichment_elements";
      anchor_block_id: string;
      order: EditorialEnrichmentElementRef[];
    };

export interface EditorialVisualRevisionProposalPatch {
  schema_version: "editorial-enrichment-visual-patch-v1";
  operation: EditorialProposalOperation;
  target: Record<string, unknown>;
  target_id: string;
  occurrence_id: string;
  before_hash: string;
  visual_patch: EditorialVisualRevisionPatch;
  rationale: string;
}

export type EditorialProposalPatchDocument =
  EditorialProposalPatch | EditorialVisualRevisionProposalPatch;

/** Proposal read returned by the generic enrichment revision endpoint. */
export interface EditorialVisualRevisionProposalRead extends Omit<
  EditorialProposalRead,
  "patch"
> {
  patch: EditorialVisualRevisionProposalPatch | null;
}

export interface EditorialManualTableDraft {
  key: string;
  kind: EditorialEnrichmentTableKind;
  title: string;
  caption?: string | null;
  columns: EditorialTableColumn[];
  rows: Array<{
    cells: string[];
    evidence_refs: EditorialEvidenceReference[];
  }>;
  purpose: Omit<EditorialTablePurpose, "evidence_handles"> & {
    evidence_refs: EditorialEvidenceReference[];
  };
}

export interface EditorialManualTableProposalInput {
  snapshot_id: string;
  anchor_block_id: string;
  table: EditorialManualTableDraft;
}

export interface EditorialManualBarChartDraft {
  key: string;
  title: string;
  caption?: string | null;
  category_axis_label: string;
  value_axis_label: string;
  unit: string;
  points: Array<{
    label: string;
    series: string;
    value: string;
    evidence_refs: EditorialEvidenceReference[];
  }>;
  purpose: {
    question: string;
    available_data: string;
    comprehension_gain: string;
    scope: string;
    evidence_refs: EditorialEvidenceReference[];
    knowledge_limits: string;
    placement_reason: string;
  };
}

export interface EditorialManualBarChartProposalInput {
  snapshot_id: string;
  anchor_block_id: string;
  chart: EditorialManualBarChartDraft;
}

export interface EditorialManualSourceFigureSelection {
  figure_id: string;
  caption: string;
  order: number;
}

export interface EditorialManualSourceFigureProposalInput {
  snapshot_id: string;
  anchor_block_id: string;
  figures: EditorialManualSourceFigureSelection[];
}

export interface EditorialEnrichmentElementRef {
  kind: EditorialEnrichmentElementKind;
  key: string;
}

export interface EditorialVisualPurposeInput {
  question: string;
  available_data: string;
  comprehension_gain: string;
  scope: string;
  evidence_refs: EditorialEvidenceReference[];
  knowledge_limits: string;
  placement_reason: string;
}

export interface EditorialVisualChartPointInput {
  label: string;
  series: string;
  value?: string;
  date?: string;
  evidence_refs: EditorialEvidenceReference[];
}

export interface EditorialVisualChartInput {
  key: string;
  kind: "bar" | "timeline";
  title: string;
  caption?: string | null;
  category_axis_label?: string | null;
  value_axis_label?: string | null;
  unit?: string | null;
  points: EditorialVisualChartPointInput[];
  purpose: EditorialVisualPurposeInput;
}

export type EditorialDiagramNodeRole =
  | "actor"
  | "victim"
  | "malware_tool"
  | "infrastructure"
  | "data_artifact"
  | "technique_step"
  | "unknown"
  | "protocol_participant"
  | "protocol_state"
  | "protocol_field"
  | "protocol_note";

export type EditorialDiagramRelationDirection = "directed" | "undirected";

export type EditorialDiagramProfile =
  | "flow"
  | "architecture"
  | "relationship"
  | "protocol_sequence"
  | "protocol_state_machine"
  | "protocol_message_format";

export type EditorialDiagramKind =
  | "infection_chain"
  | "network_flow"
  | "execution_sequence"
  | "component_relationship"
  | "infrastructure"
  | "custom";

export type EditorialDiagramDirection = "left_to_right" | "top_to_bottom";

export interface EditorialVisualDiagramNodeInput {
  node_id: string;
  label: string;
  role?: EditorialDiagramNodeRole;
  evidence_refs?: EditorialEvidenceReference[];
}

export interface EditorialVisualDiagramEdgeInput {
  source_node_id: string;
  target_node_id: string;
  label?: string | null;
  direction?: EditorialDiagramRelationDirection | null;
  evidence_refs?: EditorialEvidenceReference[];
}

export interface EditorialVisualDiagramGroupInput {
  group_id: string;
  label: string;
  node_ids: string[];
}

export interface EditorialVisualDiagramInput {
  key: string;
  kind: EditorialDiagramKind;
  title: string;
  caption?: string | null;
  direction: EditorialDiagramDirection;
  profile?: EditorialDiagramProfile | null;
  nodes: EditorialVisualDiagramNodeInput[];
  edges?: EditorialVisualDiagramEdgeInput[];
  groups?: EditorialVisualDiagramGroupInput[];
  purpose: EditorialVisualPurposeInput;
}

/**
 * One typed revision of an existing enrichment element. The event union is
 * discriminated by `operation` and mirrors the backend Pydantic union: opaque
 * element identities only, no URLs, no free-form target paths.
 */
export type EditorialEnrichmentRevisionInput =
  | {
      snapshot_id: string;
      operation: "update_visual_caption";
      element_kind: EditorialEnrichmentElementKind;
      element_key: string;
      caption: string | null;
    }
  | {
      snapshot_id: string;
      operation: "move_enrichment_element";
      element_kind: EditorialEnrichmentElementKind;
      element_key: string;
      anchor_block_id: string;
    }
  | {
      snapshot_id: string;
      operation: "reorder_enrichment_elements";
      anchor_block_id: string;
      order: EditorialEnrichmentElementRef[];
    }
  | {
      snapshot_id: string;
      operation: "replace_source_figure";
      element_key: string;
      figure_id: string;
      caption?: string | null;
    }
  | {
      snapshot_id: string;
      operation: "remove_enrichment_element";
      element_kind: EditorialEnrichmentElementKind;
      element_key: string;
    }
  | {
      snapshot_id: string;
      operation: "revise_chart";
      element_key: string;
      chart: EditorialVisualChartInput;
    }
  | {
      snapshot_id: string;
      operation: "revise_diagram";
      element_key: string;
      diagram: EditorialVisualDiagramInput;
    };

export interface EditorialManualSourceFigurePatchReference {
  figure_id: string;
  caption: string;
  order: number;
  sha256: string;
  mime_type: string;
  byte_size: number;
  width: number;
  height: number;
  source_document_id: string;
  source_url: string;
  provenance: string;
}

export interface EditorialProposalRead {
  id: string;
  subject_id: string;
  production_run_id: string;
  occurrence_id: string;
  thread_id: string | null;
  origin: "bridge_draft" | "manual_patch";
  state: EditorialProposalState;
  state_version: number;
  operation: EditorialProposalOperation;
  base_artifact_id: string;
  base_artifact_version: number;
  base_canonical_sha256: string;
  base_text: string | null;
  patch_sha256: string | null;
  patch: EditorialProposalPatch | null;
  diagnostics: Record<string, unknown>;
  created_at: string;
}

export interface EditorialProposalDecisionResult {
  id: string;
  state: "rejected";
  state_version: number;
}

export interface EditorialProposalAcceptResult {
  id: string;
  state: "accepted";
  state_version: number;
  artifact_id: string;
  artifact_version: number;
  snapshot_id: string;
  impact_id: string;
  impact_state: "pending" | "running" | "done" | "failed" | "superseded";
}

export type EditorialManualEditOperation =
  "rewrite_title" | "rewrite_lead" | "rewrite_paragraph";

export type EditorialManualEditResult = EditorialProposalAcceptResult;

const EDITORIAL_THREAD_TRANSPORT_UNCERTAIN_MESSAGE =
  "L’état de votre envoi est incertain. Consultez le fil avant de continuer.";

const ERROR_MESSAGES: Record<string, string> = {
  publication_kind_unresolved:
    "Le type de publication est indéterminé. L’atelier reste en lecture seule.",
  editorial_history_cursor_invalid:
    "La page d’historique demandée n’est plus valide. Rechargez l’historique.",
  editorial_history_limit_invalid:
    "Le nombre de révisions demandé est invalide.",
  publication_preview_regeneration_unavailable:
    "La régénération du PDF est indisponible. Vérifiez la configuration Typst, puis réessayez.",
  publication_preview_generation_failed:
    "La génération du PDF a échoué. Vérifiez la configuration Typst, puis réessayez.",
  editorial_composition_status_unavailable:
    "L’état de la composition est temporairement indisponible.",
  editorial_snapshot_stale: "Le document a changé. Rechargez l’atelier.",
  editorial_identity_ambiguous:
    "L’identité de ce bloc historique est ambiguë. Il reste en lecture seule.",
  editorial_block_missing: "Ce bloc n’existe plus dans cette version.",
  editorial_before_hash_mismatch:
    "Ce bloc a changé. Rechargez l’atelier avant de réessayer.",
  editorial_anchor_stale:
    "Le paragraphe d’ancrage a changé ou a été supprimé. Actualisez l’atelier, puis choisissez un paragraphe disponible.",
  editorial_registry_conflict:
    "La continuité de ce bloc ne peut pas être prouvée. Une revue est nécessaire.",
  thread_not_found: "Discussion introuvable.",
  thread_binding_stale: "Ce paragraphe a changé. Rechargez l’atelier.",
  occurrence_ambiguous: "L’identité de ce paragraphe historique est ambiguë.",
  thread_body_unavailable:
    "Le contenu de cette discussion est temporairement indisponible.",
  thread_body_hash_mismatch:
    "Le contenu de cette discussion ne correspond plus à son empreinte.",
  proposal_not_found: "Proposition introuvable.",
  proposal_patch_unavailable:
    "Le contenu de cette proposition est temporairement indisponible.",
  proposal_patch_hash_mismatch:
    "Le contenu vérifié de cette proposition ne correspond plus.",
  proposal_base_unavailable:
    "Le texte de référence de cette proposition est indisponible.",
  proposal_base_hash_mismatch:
    "Le texte de référence ne correspond plus à son empreinte.",
  proposal_state_conflict:
    "La proposition a changé. Actualisez-la avant de décider.",
  proposal_base_stale:
    "Cette proposition repose sur une version qui a changé. Elle n’a pas été appliquée. Actualisez le document, puis comparez et formulez une nouvelle proposition.",
  manual_edit_no_change:
    "Le texte n’a pas changé. Aucune nouvelle version n’a été créée.",
  manual_edit_stale:
    "Le document a changé. Actualisez l’atelier, puis réappliquez votre brouillon local.",
  manual_edit_new_fact_unadmitted:
    "Cette nouvelle valeur technique n'est pas étayée par les preuves disponibles. Modifiez le passage ou ajoutez une source autorisée.",
  manual_edit_patch_invalid:
    "La proposition ne respecte pas le format éditorial autorisé. Vérifiez son contenu et réessayez.",
  manual_table_request_invalid:
    "Le tableau est incomplet ou incohérent. Vérifiez les intitulés, les cellules et les preuves de chaque ligne.",
  manual_bar_chart_request_invalid:
    "La requête de graphique à barres est invalide. Vérifiez les points, les valeurs et les preuves citées.",
  bar_chart_insufficient_data:
    "Aucun graphique quantitatif vérifiable ne peut être créé à partir des preuves actuelles. Un tableau descriptif peut être proposé à la place.",
  bar_chart_value_not_evidenced:
    "Aucun graphique quantitatif vérifiable ne peut être créé à partir des preuves actuelles. Un tableau descriptif peut être proposé à la place.",
  manual_table_analytic_question_duplicate:
    "Une question analytique équivalente existe déjà. Modifiez la consigne du tableau avant de proposer une nouvelle insertion.",
  enrichment_revision_patch_invalid:
    "La révision visuelle est invalide. Vérifiez les champs, puis réessayez.",
  enrichment_element_not_found:
    "Cet élément enrichi n’existe plus dans cette version.",
  enrichment_element_kind_mismatch:
    "Le type de cet élément enrichi ne correspond pas à la révision demandée.",
  enrichment_target_stale:
    "Cet élément enrichi a changé. Rechargez l’atelier avant de réessayer.",
  enrichment_placement_changed:
    "L’emplacement de cet élément a changé. Rechargez l’atelier.",
  enrichment_caption_invalid:
    "La légende proposée ne respecte pas les règles de l’atelier.",
  enrichment_move_anchor_invalid: "Le paragraphe de destination est invalide.",
  enrichment_order_invalid:
    "L’ordre demandé ne correspond pas aux éléments de ce paragraphe.",
  enrichment_order_group_unsupported:
    "Ce groupe d’éléments ne peut pas être réordonné.",
  enrichment_revision_no_change:
    "La révision ne modifie rien. Aucune nouvelle version n’a été créée.",
  enrichment_revision_unsupported_operation:
    "Cette opération de révision visuelle n’est pas encore disponible.",
  editorial_caption_text_violation:
    "La légende contient un format ou un balisage non autorisé.",
  editorial_figure_not_changed:
    "La figure sélectionnée est déjà celle de cet élément.",
  editorial_figure_not_eligible:
    "Cette figure archivée ne peut pas être intégrée.",
  editorial_figure_not_found:
    "Cette figure n’est plus disponible dans l’inventaire.",
  brief_figure_limit_exceeded:
    "Une brève ne peut contenir plus de trois figures intégrées.",
  source_figure_inventory_limit_exceeded:
    "Le nombre de figures dépasse la limite de l’inventaire archivé.",
  manual_edit_request_too_large:
    "Le tableau dépasse la taille autorisée. Réduisez son contenu avant de réessayer.",
  proposal_operation_unsupported:
    "Cette insertion n’est pas autorisée pour le paragraphe sélectionné. Actualisez l’atelier.",
  proposal_target_mismatch:
    "La cible de cette insertion a changé. Actualisez l’atelier et recommencez.",
  manual_edit_request_invalid: "La requête de modification est invalide.",
  manual_edit_unavailable:
    "Cette opération éditoriale est temporairement indisponible. Actualisez l’atelier avant de réessayer.",
  editorial_proposal_accept_unavailable:
    "L’acceptation des propositions est temporairement indisponible.",
  proposal_idempotency_conflict:
    "Cette clé est déjà associée à une autre décision. Actualisez l’atelier, puis réessayez.",
  propose_not_enabled: "La proposition n’est pas encore activée.",
  editorial_thread_busy: "Une réponse est déjà en cours pour ce paragraphe.",
  external_llm_blocked: "La politique interdit l’envoi externe de ce contenu.",
  bridge_unavailable:
    "Le Bridge est indisponible. Actualisez avant de continuer.",
  reconciliation_required:
    "L’état de votre envoi est incertain. Consultez le fil avant de continuer.",
  idempotency_conflict: "Cette clé est déjà associée à une autre demande.",
  idempotency_key_required:
    "Une clé d’idempotence est requise pour envoyer ce message.",
  idempotency_key_invalid: "La clé d’idempotence doit être un UUID valide.",
  editorial_thread_session_not_lost:
    "La session actuelle est encore disponible. Aucune nouvelle discussion n’a été créée.",
  session_lost:
    "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre",
  editorial_message_invalid: "Saisissez un message avant de l’envoyer.",
  draft_not_found: "Aucun brouillon serveur n’est enregistré pour ce bloc.",
  draft_expired: "Ce brouillon a expiré. Rechargez le document.",
  draft_version_conflict:
    "Le brouillon a changé. Rechargez-le avant de continuer.",
  draft_base_stale:
    "Le document a changé depuis ce brouillon. Rechargez l’atelier.",
  draft_retention_unconfigured:
    "La conservation des brouillons n’est pas configurée.",
  draft_storage_unavailable:
    "Le stockage du brouillon est temporairement indisponible.",
  draft_cleanup_unavailable:
    "La purge du brouillon est temporairement indisponible.",
  draft_text_too_large: "Le brouillon dépasse la taille autorisée.",
  draft_if_match_required:
    "Une version de brouillon est requise pour cette modification.",
  draft_if_match_invalid: "La version de brouillon est invalide.",
  draft_request_invalid: "La requête de brouillon est invalide.",
  editorial_thread_transport_uncertain:
    EDITORIAL_THREAD_TRANSPORT_UNCERTAIN_MESSAGE,
};

interface ErrorDetail extends Record<string, unknown> {
  code?: string;
  message?: string;
  expires_at?: string;
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
  return new ApiError(message, code, response.status, detailObject);
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw await adaptApiError(response);
  return (await response.json()) as T;
}

export function getEditorialServerDraft(
  subjectId: string,
  occurrenceId: string,
): Promise<EditorialServerDraft> {
  return request<EditorialServerDraft>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(occurrenceId)}/draft`,
    { cache: "no-store" },
  );
}

export function putEditorialServerDraft(
  subjectId: string,
  occurrenceId: string,
  input: {
    snapshot_id: string;
    text: string;
    expected_version: number;
  },
): Promise<EditorialServerDraft> {
  return request<EditorialServerDraft>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(occurrenceId)}/draft`,
    {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "If-Match": `"${input.expected_version}"`,
      },
      body: JSON.stringify({
        snapshot_id: input.snapshot_id,
        text: input.text,
      }),
      cache: "no-store",
    },
  );
}

export async function deleteEditorialServerDraft(
  subjectId: string,
  occurrenceId: string,
  expectedVersion: number,
): Promise<void> {
  const response = await fetch(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(occurrenceId)}/draft`,
    {
      method: "DELETE",
      headers: { "If-Match": `"${expectedVersion}"` },
      cache: "no-store",
    },
  );
  if (!response.ok) throw await adaptApiError(response);
}

export function getEditorialWorkspaceSnapshot(
  subjectId: string,
): Promise<EditorialWorkspaceSnapshot> {
  return request<EditorialWorkspaceSnapshot>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/snapshot`,
  );
}

export function getEditorialPdfStatus(
  subjectId: string,
): Promise<EditorialPdfStatusView> {
  return request<EditorialPdfStatusView>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/pdf`,
    { cache: "no-store" },
  );
}

export function regenerateEditorialPdf(
  subjectId: string,
): Promise<EditorialPdfStatusView> {
  return request<EditorialPdfStatusView>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/pdf/regenerate`,
    { method: "POST" },
  );
}

export interface EditorialHistoryFilters {
  occurrenceId?: string;
  cursor?: string;
  limit?: number;
}

export function getEditorialHistory(
  subjectId: string,
  filters: EditorialHistoryFilters = {},
): Promise<EditorialHistoryPage> {
  const params = new URLSearchParams();
  if (filters.occurrenceId) params.set("block_id", filters.occurrenceId);
  if (filters.cursor) params.set("cursor", filters.cursor);
  if (filters.limit !== undefined) params.set("limit", String(filters.limit));
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return request<EditorialHistoryPage>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/history${query}`,
    { cache: "no-store" },
  );
}

export function getEditorialCompositionStatus(
  subjectId: string,
): Promise<EditorialCompositionStatusView> {
  return request<EditorialCompositionStatusView>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/composition`,
    { cache: "no-store" },
  );
}

export function getEditorialFigureCatalog(
  subjectId: string,
  filters: EditorialFigureCatalogFilters,
  offset: number,
  limit = 30,
): Promise<EditorialFigureCatalogPage> {
  const params = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  });
  if (filters.status) params.set("status", filters.status);
  if (filters.source) params.set("source", filters.source);
  if (filters.q) params.set("q", filters.q);
  return request<EditorialFigureCatalogPage>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/figures?${params.toString()}`,
    { cache: "no-store" },
  );
}

export function editorialFigureThumbnailUrl(
  subjectId: string,
  figureId: string,
): string {
  return `/api/subjects/${encodeURIComponent(subjectId)}/editor/figures/${encodeURIComponent(figureId)}/thumbnail`;
}

export function getEditorialFigurePreview(
  subjectId: string,
  figureId: string,
): Promise<EditorialFigurePreview> {
  return request<EditorialFigurePreview>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/figures/${encodeURIComponent(figureId)}/preview`,
    { cache: "no-store" },
  );
}

export function getEditorialProposal(
  subjectId: string,
  occurrenceId: string,
  proposalId: string,
): Promise<EditorialProposalRead> {
  return request<EditorialProposalRead>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/proposals/${encodeURIComponent(proposalId)}`,
  );
}

export function rejectEditorialProposal(
  subjectId: string,
  occurrenceId: string,
  proposalId: string,
  decision: { expected_state_version: number; patch_sha256: string },
): Promise<EditorialProposalDecisionResult> {
  return request<EditorialProposalDecisionResult>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/proposals/${encodeURIComponent(proposalId)}/reject`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify(decision),
    },
  );
}

export function acceptEditorialProposal(
  subjectId: string,
  occurrenceId: string,
  proposalId: string,
  decision: { expected_state_version: number; patch_sha256: string },
): Promise<EditorialProposalAcceptResult> {
  return request<EditorialProposalAcceptResult>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/proposals/${encodeURIComponent(proposalId)}/accept`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify(decision),
    },
  );
}

export function createManualEditorialTableProposal(
  subjectId: string,
  anchorBlockId: string,
  idempotencyKey: string,
  input: EditorialManualTableProposalInput,
): Promise<EditorialProposalRead> {
  return request<EditorialProposalRead>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(anchorBlockId)}/table-proposals`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(input),
    },
  );
}

export function createManualEditorialBarChartProposal(
  subjectId: string,
  anchorBlockId: string,
  idempotencyKey: string,
  input: EditorialManualBarChartProposalInput,
): Promise<EditorialProposalRead> {
  return request<EditorialProposalRead>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(anchorBlockId)}/bar-chart-proposals`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(input),
    },
  );
}

export function createManualEditorialSourceFigureProposal(
  subjectId: string,
  anchorBlockId: string,
  idempotencyKey: string,
  input: EditorialManualSourceFigureProposalInput,
): Promise<EditorialProposalRead> {
  return request<EditorialProposalRead>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(anchorBlockId)}/figure-proposals`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(input),
    },
  );
}

export function prepareEnrichmentRevision(
  subjectId: string,
  occurrenceId: string,
  idempotencyKey: string,
  input: EditorialEnrichmentRevisionInput,
): Promise<EditorialVisualRevisionProposalRead> {
  return request<EditorialVisualRevisionProposalRead>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(occurrenceId)}/enrichment-revisions`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(input),
      cache: "no-store",
    },
  );
}

export function saveManualEditorialEdit(
  subjectId: string,
  occurrenceId: string,
  idempotencyKey: string,
  input: {
    snapshot_id: string;
    operation: EditorialManualEditOperation;
    text: string;
  },
): Promise<EditorialManualEditResult> {
  return request<EditorialManualEditResult>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/blocks/${encodeURIComponent(occurrenceId)}/manual-edit`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(input),
      cache: "no-store",
    },
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

export async function getEditorialThread(
  subjectId: string,
  occurrenceId: string,
  options: { after_sequence?: number; etag?: string } = {},
): Promise<EditorialThreadRead | null> {
  const parameters = new URLSearchParams();
  if (options.after_sequence !== undefined) {
    parameters.set("after_sequence", String(options.after_sequence));
  }
  const query = parameters.size > 0 ? `?${parameters.toString()}` : "";
  const headers = new Headers();
  if (options.etag) headers.set("If-None-Match", options.etag);
  const response = await fetch(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}${query}`,
    { headers },
  );
  if (response.status === 304) return null;
  if (!response.ok) throw await adaptApiError(response);
  const payload = (await response.json()) as EditorialThreadRead;
  return { ...payload, etag: response.headers.get("ETag") ?? payload.etag };
}

export async function sendEditorialThreadMessage(
  subjectId: string,
  occurrenceId: string,
  text: string,
  kind: "message" | "propose" = "message",
): Promise<EditorialThreadSendResult> {
  const idempotencyKey = crypto.randomUUID();
  const url = `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/entries`;
  let transportAttempt = 0;
  while (true) {
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({ kind, text }),
      });
      if (!response.ok) throw await adaptApiError(response);
      return (await response.json()) as EditorialThreadSendResult;
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
      if (transportAttempt >= 2) {
        throw new ApiError(
          EDITORIAL_THREAD_TRANSPORT_UNCERTAIN_MESSAGE,
          "editorial_thread_transport_uncertain",
          0,
        );
      }
      transportAttempt += 1;
      await new Promise((resolve) =>
        setTimeout(resolve, 100 * transportAttempt),
      );
    }
  }
}

export async function reconcileEditorialThread(
  subjectId: string,
  occurrenceId: string,
): Promise<EditorialThreadReconciliationResult> {
  return request<EditorialThreadReconciliationResult>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/reconcile`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    },
  );
}

export async function startNewEditorialThreadSession(
  subjectId: string,
  occurrenceId: string,
  idempotencyKey: string,
): Promise<EditorialThreadSessionStartResult> {
  return request<EditorialThreadSessionStartResult>(
    `/api/subjects/${encodeURIComponent(subjectId)}/editor/threads/${encodeURIComponent(occurrenceId)}/entries`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({ kind: "message", start_new_session: true }),
    },
  );
}
