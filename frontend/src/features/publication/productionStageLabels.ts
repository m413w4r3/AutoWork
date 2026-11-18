import type {
  ProductionExtractionTierV1,
  ProductionExtractionReuseStateV1,
  ProductionSourceExtractionV1,
} from "../../api/production";

export const REUSE_STATE_LABELS: Record<
  ProductionExtractionReuseStateV1,
  string
> = {
  fresh: "Calculée",
  reused: "Réutilisée",
  duplicate_content: "Contenu identique à une autre source",
  content_duplicate: "Contenu identique à une autre source",
};

export const REFERENCE_TIER_LABELS: Record<ProductionExtractionTierV1, string> =
  {
    core: "Core",
    supporting: "Référence complémentaire",
    technical: "Ressource technique",
  };

export const REFERENCE_ROLE_LABELS: Record<string, string> = {
  primary: "Primaire",
  independent: "Indépendante",
  relay: "Relais",
  aggregator: "Agrégateur",
  social: "Réseau social",
  unknown: "Inconnu",
};

export const REFERENCE_KIND_LABELS: Record<
  ProductionSourceExtractionV1["kind"],
  string
> = {
  publication: "Publication",
  technical_resource: "Ressource technique",
};
