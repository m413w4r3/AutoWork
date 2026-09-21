import type {
  ProductionBatchPhase,
  ProductionStage,
  ProductionRunStatus,
} from "../../api/production";

export const STATUS_LABELS: Record<ProductionRunStatus, string> = {
  queued: "En attente",
  running: "En cours",
  ready: "Prêt",
  needs_review: "À vérifier",
  failed: "Échec",
  cancelled: "Annulé",
};

export const STAGE_LABELS: Record<ProductionStage, string> = {
  sources: "Sources",
  references: "Références",
  extraction: "Extraction",
  synthesis: "Synthèse",
  assembly: "Assemblage",
};

export const PHASE_LABELS: Record<ProductionBatchPhase, string> = {
  initial: "Production initiale",
  recovery: "Récupération automatique",
  review: "Finalisation",
};
