import { subjectPublicationPdfUrl } from "../../api/subjectContent";
import type { EditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";
import { navigate } from "../../navigation";

interface EditorialTopBarProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  stale: boolean;
  hasLocalDrafts?: boolean;
  hasStaleLocalDrafts?: boolean;
  menuOpen: boolean;
  technicalOpen: boolean;
  onToggleMenu: (button: HTMLButtonElement) => void;
  onOpenTechnicalDetails: () => void;
  onTechnicalOpenChange: (open: boolean) => void;
  onRefresh: () => void;
  onNavigateAway?: () => boolean;
}

function technicalDetailRows(snapshot: EditorialWorkspaceSnapshot) {
  return [
    ["Identifiant du snapshot", snapshot.snapshot_id],
    ["Identifiant du sujet", snapshot.subject_id],
    ["Identifiant du run", snapshot.production_run_id],
    ["Artefact de synthèse", snapshot.synthesis_artifact_id],
    ["SHA-256 canonique de la synthèse", snapshot.synthesis_canonical_sha256],
    ["Version de synthèse", String(snapshot.synthesis_version)],
    ["Artefact d’enrichissement", snapshot.enrichment_artifact_id ?? "—"],
    ["Version d’enrichissement", String(snapshot.enrichment_version ?? "—")],
    ["Artefact de publication", snapshot.publication_artifact_id ?? "—"],
    ["Version de publication", String(snapshot.publication_version ?? "—")],
    ["Génération du pipeline", String(snapshot.pipeline_generation)],
  ] as const;
}

export function EditorialTopBar({
  subjectId,
  snapshot,
  stale,
  hasLocalDrafts = false,
  hasStaleLocalDrafts = false,
  menuOpen,
  technicalOpen,
  onToggleMenu,
  onOpenTechnicalDetails,
  onTechnicalOpenChange,
  onRefresh,
  onNavigateAway = () => true,
}: EditorialTopBarProps) {
  const title =
    snapshot.blocks.find((block) => block.kind === "title")?.text?.trim() ||
    "Document éditorial";
  const shortTitle =
    title.length > 64 ? `${title.slice(0, 61).trimEnd()}…` : title;
  const statusLabel =
    snapshot.preview_status === "STALE"
      ? "PDF obsolète"
      : stale
        ? "Version périmée"
        : hasStaleLocalDrafts
          ? "Version périmée"
          : hasLocalDrafts
            ? "Brouillon local"
            : snapshot.preview_status === "IN_PROGRESS"
              ? "Aperçu en cours"
              : snapshot.preview_status === "FAILED"
                ? "PDF indisponible"
                : "À jour";
  const statusDescription =
    snapshot.preview_status === "STALE"
      ? snapshot.impact_status === "failed"
        ? "Le traitement de l’impact sera retenté automatiquement et peut être rejoué sans annuler l’édition. La prévisualisation PDF se relance depuis l’écran d’aperçu de l’édition."
        : snapshot.impact_status === "pending" ||
            snapshot.impact_status === "running"
          ? "Le traitement de l’impact est en cours. La prévisualisation PDF se relance depuis l’écran d’aperçu de l’édition."
          : "La publication affichée est périmée. La prévisualisation PDF se relance depuis l’écran d’aperçu de l’édition."
      : stale
        ? "Le document a changé. Actualisez le statut de l’atelier."
        : hasStaleLocalDrafts
          ? "La version du document a changé depuis ce brouillon. Il est conservé et n’a pas été appliqué à la version actuelle."
          : hasLocalDrafts
            ? "Un brouillon de ce bloc n’est pas encore enregistré comme nouvelle version canonique."
            : snapshot.preview_status === "IN_PROGRESS"
              ? "Le rendu de l’aperçu est en cours. Le document reste consultable."
              : snapshot.preview_status === "FAILED"
                ? "L’aperçu PDF est indisponible."
                : "Le document affiché correspond à la dernière version canonique enregistrée.";
  const publicationKindLabel =
    snapshot.publication_kind === "article"
      ? "Article"
      : snapshot.publication_kind === "brief"
        ? "Brève"
        : "Type indéterminé";
  const canOpenPdf =
    snapshot.capabilities.can_preview_pdf &&
    snapshot.publication_artifact_id !== null &&
    snapshot.publication_version !== null &&
    !stale;

  return (
    <header className="editorial-topbar">
      <div className="editorial-topbar__identity">
        <div className="editorial-topbar__back">
          <a
            href={`/subjects/${subjectId}`}
            onClick={(event) => {
              if (
                event.button !== 0 ||
                event.metaKey ||
                event.ctrlKey ||
                event.shiftKey ||
                event.altKey
              ) {
                return;
              }
              event.preventDefault();
              if (onNavigateAway()) navigate(`/subjects/${subjectId}`);
            }}
          >
            ← Retour au sujet
          </a>
        </div>
        <div>
          <p className="editorial-topbar__subject">Atelier éditorial</p>
          <h1 id="editorial-workspace-heading" aria-label={title} title={title}>
            {shortTitle}
          </h1>
        </div>
      </div>
      <div className="editorial-topbar__metadata">
        <span className="editorial-kind">{publicationKindLabel}</span>
        <span className="editorial-version">
          Version canonique v
          {snapshot.publication_version ?? snapshot.synthesis_version}
        </span>
        <span
          className={
            "editorial-status" +
            (stale || hasStaleLocalDrafts ? " editorial-status--attention" : "")
          }
          role="status"
          aria-live="polite"
          aria-label={`${statusLabel}. ${statusDescription}`}
        >
          {statusLabel}
        </span>
        {canOpenPdf ? (
          <a
            className="button button--secondary editorial-topbar__pdf"
            href={subjectPublicationPdfUrl(subjectId)}
            target="_blank"
            rel="noreferrer"
          >
            Voir le PDF
          </a>
        ) : (
          <button
            className="button button--secondary editorial-topbar__pdf"
            type="button"
            disabled
            title="Aucun aperçu PDF disponible pour cette version."
          >
            Voir le PDF
          </button>
        )}
        <div className="editorial-topbar__menu-wrap">
          <button
            className="button button--secondary"
            type="button"
            aria-label="Historique et détails"
            aria-expanded={menuOpen}
            aria-haspopup="menu"
            onClick={(event) => onToggleMenu(event.currentTarget)}
          >
            Historique et détails
          </button>
          {menuOpen ? (
            <div className="editorial-topbar__menu" role="menu">
              <button
                type="button"
                role="menuitem"
                disabled
                title="L’historique sera disponible dans une prochaine étape."
              >
                Historique
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={onOpenTechnicalDetails}
              >
                Détails techniques
              </button>
            </div>
          ) : null}
        </div>
      </div>
      {snapshot.publication_kind === null ? (
        <p className="editorial-readonly-note" role="note">
          Le type de publication est indéterminé. L’atelier reste en lecture
          seule.
        </p>
      ) : null}
      {stale ? (
        <div className="editorial-stale-notice" role="status">
          <p>{statusDescription}</p>
          <button
            className="button button--secondary"
            type="button"
            onClick={onRefresh}
          >
            Actualiser le statut
          </button>
        </div>
      ) : null}
      <details
        className="editorial-technical-details"
        open={technicalOpen}
        onToggle={(event) => onTechnicalOpenChange(event.currentTarget.open)}
      >
        <summary>Détails techniques</summary>
        <dl>
          {technicalDetailRows(snapshot).map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      </details>
    </header>
  );
}
