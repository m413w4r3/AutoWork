import type {
  AnalystObservationRelation,
  AnalystObservationState,
  NoteProposalStatus,
  NoteTargetBlockType,
  ProseOpinionBlockType,
} from "../api/opinionProposals";
import type { OpinionVersionV1 } from "./types";

export const BLOCK_LABELS: Record<ProseOpinionBlockType, string> = {
  key_findings: "Apports de l’enquête",
  additional_samples: "Échantillons complémentaires",
  infection_chain_and_capabilities: "Chaîne d’infection et capacités",
  network_and_infrastructure: "Activité réseau et infrastructure",
  claim_verification: "Vérification des affirmations",
  proposed_detections: "Détections proposées",
  uncertainties_and_next_leads: "Incertitudes et prochaines pistes",
};

export const BLOCK_ORDER: ProseOpinionBlockType[] = [
  "key_findings",
  "additional_samples",
  "infection_chain_and_capabilities",
  "network_and_infrastructure",
  "claim_verification",
  "proposed_detections",
  "uncertainties_and_next_leads",
];

export const NOTE_STATE_LABELS: Record<AnalystObservationState, string> = {
  observed: "Observé",
  inferred: "Inféré",
  unresolved: "Non résolu",
};

export const NOTE_RELATION_LABELS: Record<AnalystObservationRelation, string> =
  {
    confirms: "Confirme",
    refutes: "Réfute",
    qualifies: "Nuance",
    unresolved: "Sans relation tranchée",
  };

export const OPINION_STATUS_LABELS: Record<OpinionVersionV1["status"], string> =
  {
    draft: "Brouillon",
    approved: "Approuvé",
    rejected: "Refusé",
  };

export const PROPOSAL_STATUS_LABELS: Record<NoteProposalStatus, string> = {
  proposed: "Proposée",
  accepted: "Acceptée",
  rejected: "Refusée",
  stale: "Périmée",
  cancelled: "Annulée",
};

export const TARGET_BLOCK_LABELS: Record<NoteTargetBlockType, string> = {
  lead: "Chapô de l’article",
  section: "Section de l’article",
};

export const CONSULTATION_LABELS: Record<string, string> = {
  draft_text: "Rédaction",
  review_text: "Vérification",
};

export const QUALIFICATION_LABELS: Record<string, string> = {
  fact: "Fait",
  inference: "Inférence",
  hypothesis: "Hypothèse",
};

export const PROTOCOL_FIGURE_LABELS: Record<string, string> = {
  sequence: "Séquence des échanges",
  state_machine: "Machine à états",
  message_format: "Format des messages",
};

export const FINDING_KIND_LABELS: Record<string, string> = {
  new_sample: "Nouvel échantillon",
  infection_chain: "Chaîne d’infection",
  capability: "Capacité",
  network_behaviour: "Comportement réseau",
  claim_verification: "Revendication vérifiée",
  detection: "Détection",
};

export const EVIDENCE_KIND_LABELS: Record<string, string> = {
  vt_file_report: "Rapport VT fichier",
  vt_relationship: "Relation VT",
  vt_behaviour: "Comportement VT",
  shodan_observation: "Observation Shodan",
  binary_analysis: "Analyse binaire",
  binary_string: "Chaîne binaire",
  yara_match: "Correspondance YARA",
  negative_result: "Résultat négatif",
  claim_source: "Source de revendication",
  other: "Autre observation",
};

export function shortHash(value: string | null | undefined): string {
  if (value === null || value === undefined || value.length === 0) return "—";
  return `${value.slice(0, 12)}…`;
}

export function truncate(value: string, maxLength = 160): string {
  return value.length <= maxLength ? value : `${value.slice(0, maxLength)}…`;
}
