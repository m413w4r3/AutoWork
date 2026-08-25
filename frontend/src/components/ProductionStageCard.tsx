interface ProductionStageCardProps {
  stage: string;
  status: string;
  stageNumber: number;
  isActive?: boolean;
  /** Short count line, e.g. "5 archivée(s)". */
  detail?: string;
}

const STAGE_NAMES: Record<string, string> = {
  sources: "Sources",
  references: "Références",
  extraction: "Extraction CTI",
  synthesis: "Synthèse",
  assembly: "Brève",
};

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
  detail,
}: ProductionStageCardProps) {
  return (
    <li
      className={`production-stage is-${status}${isActive ? " is-active" : ""}`}
    >
      <span className="production-stage__icon" aria-hidden="true">
        {STATUS_ICONS[status] ?? "○"}
      </span>
      <span className="production-stage__name">
        {stageNumber}. {STAGE_NAMES[stage] ?? stage}
      </span>
      <span className="production-stage__status">
        {STATUS_LABELS[status] ?? status}
        {detail ? ` · ${detail}` : ""}
      </span>
    </li>
  );
}
