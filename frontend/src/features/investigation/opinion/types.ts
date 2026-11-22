/**
 * Wire types of the analyst-note view (M24-09D).
 *
 * The frozen workshop client (`../api`) does not declare the opinion
 * `observations` list, so this module transcribes the read payload of
 * `GET /api/investigations/{id}/opinion` (08A/08B). No view code mutates the
 * opinion: a note enters the canonical version only through a human-accepted
 * Bridge proposal.
 */

import type {
  CitationViolationPayload,
  InvestigationOpinionBlock,
} from "../../../api/investigation";
import type {
  AnalystObservationRelation,
  AnalystObservationState,
  ProseOpinionBlockType,
} from "../api/opinionProposals";
import type { Uuid } from "../api/types";

export interface BridgeDraftReferenceV1 {
  invocation_id: string;
  text_sha256: string;
  source_text_sha256?: string;
  source_observation_id?: Uuid;
}

export interface BridgeReviewReferenceV1 {
  invocation_id: string;
  output_sha256: string;
  reviewed_text_sha256: string;
}

/** One `AnalystObservationV1` projected beside a stable article anchor. */
export interface AnalystNoteV1 {
  id: Uuid;
  anchor: string;
  text: string;
  state: AnalystObservationState;
  relation: AnalystObservationRelation;
  finding_refs: Uuid[];
  evidence_refs: Uuid[];
  bridge_authored?: boolean;
  bridge_block_type?: ProseOpinionBlockType | null;
  bridge_draft_ref?: BridgeDraftReferenceV1 | null;
  bridge_review_ref?: BridgeReviewReferenceV1 | null;
  bridge_modified_by_operator?: boolean;
}

export interface OpinionProvenanceV1 {
  investigation_id: Uuid;
  synthesis_artifact_id: Uuid;
  synthesis_sha256: string;
  base_publication_artifact_id: Uuid;
  base_publication_sha256: string;
}

export interface OpinionProtocolFigureV1 {
  kind: string;
  model_sha256: string;
  spec_sha256: string;
  asset_id: string;
  asset_sha256: string;
  preview_sha256: string;
  pin_sha256: string;
  compiler_name: string;
  compiler_version: string;
  compiler_policy_version: string;
  caption: string;
}

export interface ImplantMaterialsSectionV1 {
  type: "implant_materials";
  text: string;
  bridge_authored: boolean;
  bridge_modified_by_operator?: boolean;
}

export interface OpinionBridgeDraftingSummaryV1 {
  status: "complete" | "partial" | "incomplete";
  section_count: number;
  bridge_authored_section_count: number;
  unlinked_sections: string[];
  requires_acknowledgement: boolean;
  acknowledged: boolean;
  resume_available: boolean;
  diagnosis: string | null;
}

export interface OpinionEditorialReviewV1 {
  status: "clear" | "warning" | "blocked";
  bridge_drafting: OpinionBridgeDraftingSummaryV1;
  publication_text_markers: string[];
  missing_annex_references: string[];
  warnings: string[];
}

/** One `AnalystOpinionV1` payload as returned by the opinion read route. */
export interface OpinionVersionV1 {
  schema: "analyst-opinion.v1";
  id: Uuid;
  version: number;
  investigation_id: Uuid;
  synthesis_artifact_id: Uuid;
  synthesis_sha256: string;
  base_publication_artifact_id: Uuid;
  base_publication_sha256: string;
  blocks: InvestigationOpinionBlock[];
  protocol_figures?: OpinionProtocolFigureV1[];
  unresolved_points: string[];
  author_id: string;
  reviewer_id: string | null;
  status: "draft" | "approved" | "rejected";
  created_at: string;
  content_sha256: string;
  renderable_blocks: InvestigationOpinionBlock[];
  implant_materials_section?: ImplantMaterialsSectionV1 | null;
  provenance: OpinionProvenanceV1;
  editorial_review?: OpinionEditorialReviewV1;
  observations?: AnalystNoteV1[];
}

export interface OpinionReadPayloadV1 {
  draft: OpinionVersionV1 | null;
  history: OpinionVersionV1[];
  citation_violations: CitationViolationPayload[];
  renderable_blocks?: InvestigationOpinionBlock[];
  provenance?: OpinionProvenanceV1 | null;
  editorial_review?: OpinionEditorialReviewV1 | null;
}
