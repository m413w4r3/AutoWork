import type {
  EditorialImpactStatus,
  EditorialPdfStatus,
} from "../../api/editorialWorkspace";

interface EditorialWorkspaceStatusProps {
  impactStatus: EditorialImpactStatus;
  pdfStatus: EditorialPdfStatus;
  showPdfStatus?: boolean;
  showImpactDetails?: boolean;
}

const impactCopy: Record<
  EditorialImpactStatus,
  { label: string; description: string; attention: boolean }
> = {
  none: {
    label: "À jour",
    description: "Aucun impact éditorial ne nécessite de traitement.",
    attention: false,
  },
  pending: {
    label: "Impact en attente",
    description: "La mise à jour des éléments dépendants est en attente.",
    attention: true,
  },
  running: {
    label: "Impact en cours",
    description: "La mise à jour des éléments dépendants est en cours.",
    attention: true,
  },
  superseded: {
    label: "Impact périmé",
    description: "Une version plus récente a remplacé cet impact.",
    attention: true,
  },
  up_to_date: {
    label: "Impact à jour",
    description: "Les éléments dépendants ont été vérifiés pour cette version.",
    attention: false,
  },
  failed: {
    label: "Échec de l’impact",
    description:
      "La mise à jour a échoué. Un nouvel essai sera lancé automatiquement ; le traitement peut être rejoué sans annuler l’édition.",
    attention: true,
  },
};

export function EditorialWorkspaceStatus({
  impactStatus,
  pdfStatus,
  showPdfStatus = true,
  showImpactDetails = true,
}: EditorialWorkspaceStatusProps) {
  const copy = impactCopy[impactStatus];
  const hasImpact = impactStatus !== "none";

  if (!hasImpact && (!showPdfStatus || pdfStatus !== "STALE")) {
    return null;
  }

  return (
    <section
      className="editorial-workspace-status"
      aria-label="État de l’atelier"
    >
      {hasImpact ? (
        <>
          <p
            className={`editorial-workspace-status__impact${
              copy.attention
                ? " editorial-workspace-status__impact--attention"
                : ""
            }`}
            role="status"
            aria-live="polite"
            aria-atomic="true"
            aria-label={copy.description}
          >
            {copy.label}
          </p>
          {impactStatus === "failed" && showImpactDetails ? (
            <p className="editorial-workspace-status__detail">
              {copy.description}
            </p>
          ) : null}
        </>
      ) : null}
      {showPdfStatus && pdfStatus === "STALE" ? (
        <span
          className="editorial-workspace-status__pdf"
          aria-label="PDF obsolète"
          role="status"
          aria-live="polite"
        >
          PDF obsolète
        </span>
      ) : null}
    </section>
  );
}
