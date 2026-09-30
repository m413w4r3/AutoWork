import type { ProductionStage } from "../../api/production";

export const PRODUCTION_STAGE_ORDER = [
  "sources",
  "references",
  "extraction",
  "synthesis",
  "editorial_enrichment",
  "assembly",
] as const satisfies readonly ProductionStage[];

export const PRODUCTION_STAGE_LABELS: Record<ProductionStage, string> = {
  sources: "Sources",
  references: "Références",
  extraction: "Extraction",
  synthesis: "Synthèse",
  editorial_enrichment: "Enrichissement",
  assembly: "Assemblage",
};

export const RETRYABLE_PRODUCTION_STAGES: readonly ProductionStage[] =
  PRODUCTION_STAGE_ORDER;
