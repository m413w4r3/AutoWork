import { PRODUCTION_STAGE_LABELS } from "../features/production/productionStages";
import type { ProductionStage } from "../api/production";

interface ProductionStageCardProps {
  stage: ProductionStage;
  status: string;
  stageNumber: number;
  isActive?: boolean;
  reused?: boolean;
  /** Short count line, e.g. "5 archivée(s)". */
  detail?: string;
}

const STATUS_LABELS: Record<string, string> = {
  pending: "en attente",
  running: "en cours",
  succeeded: "terminée",
  verified: "terminée",
  needs_review: "à vérifier",
  failed: "en échec",
  cancelled: "annulée",
};

const STATUS_ICONS: Record<string, string> = {
  pending: "○",
  running: "●",
  succeeded: "✓",
  verified: "✓",
  needs_review: "⚠",
  failed: "✗",
  cancelled: "⊘",
};

export function ProductionStageCard({
  stage,
  status,
  stageNumber,
  isActive,
  reused = false,
  detail,
}: ProductionStageCardProps) {
  return (
    <li
      className={`production-stage is-${status}${reused ? " is-reused" : ""}${isActive ? " is-active" : ""}`}
    >
      <span className="production-stage__icon" aria-hidden="true">
        {STATUS_ICONS[status] ?? "○"}
      </span>
      <span className="production-stage__name">
        {stageNumber}. {PRODUCTION_STAGE_LABELS[stage]}
      </span>
      <span className="production-stage__status">
        {reused ? "réutilisée" : (STATUS_LABELS[status] ?? status)}
        {reused ? " · depuis un calcul précédent" : null}
        {detail ? ` · ${detail}` : ""}
      </span>
    </li>
  );
}
