import type { RepairExecutionPlan } from "../../api/publication";

const IMPACT_LABELS: Record<RepairExecutionPlan["impact_kind"], string> = {
  no_deliverable_change: "Aucun contenu à reconstruire",
  rule_bundle_only: "Mettre à jour les règles",
  publication_only: "Mettre à jour la publication",
  narrative: "Régénérer la synthèse",
  source_corpus: "Réintégrer la source",
};

const IMPACT_SUBTITLES: Record<RepairExecutionPlan["impact_kind"], string> = {
  no_deliverable_change: "La décision ne change aucun livrable.",
  rule_bundle_only:
    "Extraction effective et fichiers de règles uniquement. Aucun appel modèle.",
  publication_only:
    "Valeurs publiées et rendu final uniquement. La synthèse existante sera conservée. Aucun appel modèle.",
  narrative:
    "Cette correction change le contenu narratif. Une nouvelle synthèse sera demandée.",
  source_corpus: "Les références, l’extraction et la synthèse peuvent changer.",
};

export function repairPlanActionLabel(plan: RepairExecutionPlan): string {
  return IMPACT_LABELS[plan.impact_kind];
}

export function repairPlanSubtitle(plan: RepairExecutionPlan): string {
  return IMPACT_SUBTITLES[plan.impact_kind];
}

export function repairPlanImpactLabel(plan: RepairExecutionPlan): string {
  switch (plan.impact_kind) {
    case "no_deliverable_change":
      return "Aucun changement de livrable";
    case "rule_bundle_only":
      return "Règles uniquement";
    case "publication_only":
      return "Publication uniquement";
    case "narrative":
      return "Contenu narratif";
    case "source_corpus":
      return "Corpus source";
  }
}

export function repairPlanCostLabel(plan: RepairExecutionPlan): string {
  if (!plan.model_call_required) return "Aucun appel modèle";
  return plan.impact_kind === "source_corpus"
    ? "Nouvelle extraction et synthèse possibles"
    : "Nouvelle synthèse requise";
}
