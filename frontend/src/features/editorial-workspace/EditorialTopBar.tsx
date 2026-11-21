import { useRef } from "react";

import type {
  EditorialCompositionStatusView,
  EditorialPdfStatusView,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { navigate } from "../../navigation";

interface EditorialTopBarProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  pdfStatus?: EditorialPdfStatusView;
  pdfStatusUnavailable?: boolean;
  pdfRegenerationPending?: boolean;
  pdfRegenerationError?: string | null;
  compositionStatus?: EditorialCompositionStatusView;
  stale: boolean;
  hasLocalDrafts?: boolean;
  hasStaleLocalDrafts?: boolean;
  menuOpen: boolean;
  technicalOpen: boolean;
  onToggleMenu: (button: HTMLButtonElement) => void;
  onOpenTechnicalDetails: () => void;
  onTechnicalOpenChange: (open: boolean) => void;
  onRefresh: () => void;
  onRefreshPdfStatus?: () => void;
  onRegeneratePdf?: () => void;
  onOpenHistory?: (button: HTMLButtonElement) => void;
  onNavigateAway?: () => boolean;
}

function technicalDetailRows(
  snapshot: EditorialWorkspaceSnapshot,
  pdfStatus?: EditorialPdfStatusView,
) {
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
    ["Identifiant du PDF", pdfStatus?.artifact_id ?? "—"],
    ["Version du PDF", String(pdfStatus?.artifact_version ?? "—")],
    ["Hash des entrées du PDF", pdfStatus?.artifact_input_hash ?? "—"],
    ["SHA-256 canonique du PDF", pdfStatus?.artifact_canonical_sha256 ?? "—"],
    ["Identifiant du rendu PDF", pdfStatus?.render_id ?? "—"],
    ["Identité exacte du rendu PDF", pdfStatus?.render_identity ?? "—"],
    ["Code d’erreur PDF", pdfStatus?.error_code ?? "—"],
  ] as const;
}

export function EditorialTopBar({
  subjectId,
  snapshot,
  pdfStatus,
  pdfStatusUnavailable = false,
  pdfRegenerationPending = false,
  pdfRegenerationError = null,
  compositionStatus,
  stale,
  hasLocalDrafts = false,
  hasStaleLocalDrafts = false,
  menuOpen,
  technicalOpen,
  onToggleMenu,
  onOpenTechnicalDetails,
  onTechnicalOpenChange,
  onRefresh,
  onRefreshPdfStatus,
  onRegeneratePdf,
  onOpenHistory,
  onNavigateAway = () => true,
}: EditorialTopBarProps) {
  const historyDisclosureRef = useRef<HTMLButtonElement>(null);
  const title =
    snapshot.blocks.find((block) => block.kind === "title")?.text?.trim() ||
    "Document éditorial";
  const shortTitle =
    title.length > 64 ? `${title.slice(0, 61).trimEnd()}…` : title;
  const currentPdfStatus = pdfStatus?.status ?? snapshot.preview_status;
  const statusLabel = pdfStatusUnavailable
    ? "Statut PDF indisponible"
    : currentPdfStatus === "STALE"
      ? "PDF obsolète"
      : currentPdfStatus === "GENERATING"
        ? "Génération du PDF en cours"
        : currentPdfStatus === "FAILED"
          ? "Erreur de rendu du PDF"
          : stale || hasStaleLocalDrafts
            ? "Version périmée"
            : hasLocalDrafts
              ? "Brouillon local"
              : "À jour";
  const pdfErrorMessage =
    pdfRegenerationError ??
    pdfStatus?.error_message ??
    "La génération du PDF a échoué. Vérifiez la configuration Typst avant de relancer le rendu.";
  const statusDescription = pdfStatusUnavailable
    ? "L’état du PDF n’a pas pu être lu. Actualisez son statut avant de relancer une génération."
    : currentPdfStatus === "STALE"
      ? "Le PDF correspond à une version antérieure du document. Régénérez l’aperçu pour consulter la version actuelle."
      : currentPdfStatus === "GENERATING"
        ? "La génération du PDF est en cours. Le document reste consultable."
        : currentPdfStatus === "FAILED"
          ? pdfErrorMessage
          : stale
            ? "Le document a changé. Actualisez le statut de l’atelier."
            : hasStaleLocalDrafts
              ? "La version du document a changé depuis ce brouillon. Il est conservé et n’a pas été appliqué à la version actuelle."
              : hasLocalDrafts
                ? "Un brouillon de ce bloc n’est pas encore enregistré comme nouvelle version canonique."
                : "Le document affiché correspond à la dernière version canonique enregistrée.";
  const publicationKindLabel =
    snapshot.publication_kind === "article"
      ? "Article"
      : snapshot.publication_kind === "brief"
        ? "Brève"
        : "Type indéterminé";
  const pdfUrl = pdfStatus ? pdfStatus.pdf_url : snapshot.pdf_url;
  const canOpenPdf =
    !pdfStatusUnavailable &&
    currentPdfStatus === "READY" &&
    snapshot.capabilities.can_preview_pdf &&
    pdfUrl !== null &&
    !stale;
  const canRegeneratePdf =
    snapshot.capabilities.can_regenerate_pdf &&
    (currentPdfStatus === "STALE" ||
      currentPdfStatus === "GENERATING" ||
      currentPdfStatus === "FAILED");

  return (
    <header className="editorial-topbar">
      <div className="editorial-topbar__identity">
        <div className="editorial-topbar__back">
          <a
            href={`/subjects/${encodeURIComponent(subjectId)}`}
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
              if (onNavigateAway())
                navigate(`/subjects/${encodeURIComponent(subjectId)}`);
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
            (stale || hasStaleLocalDrafts || currentPdfStatus !== "READY"
              ? " editorial-status--attention"
              : "")
          }
          role="status"
          aria-live="polite"
          aria-atomic="true"
          aria-label={`${statusLabel}. ${statusDescription}`}
        >
          {statusLabel}
        </span>
        {canOpenPdf && pdfUrl ? (
          <a
            className="button button--secondary editorial-topbar__pdf"
            href={pdfUrl}
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
            title="Aucun aperçu PDF à jour n’est disponible pour cette version."
          >
            Voir le PDF
          </button>
        )}
        {canRegeneratePdf ? (
          <button
            className="button button--secondary editorial-topbar__pdf-regenerate"
            type="button"
            disabled={
              currentPdfStatus === "GENERATING" ||
              pdfRegenerationPending ||
              pdfStatusUnavailable
            }
            onClick={onRegeneratePdf}
          >
            {pdfRegenerationPending
              ? "Génération en cours…"
              : "Régénérer le PDF"}
          </button>
        ) : null}
        <div className="editorial-topbar__menu-wrap">
          <button
            className="button button--secondary"
            type="button"
            aria-label="Historique et détails"
            aria-expanded={menuOpen}
            aria-controls="editorial-topbar-history-actions"
            ref={historyDisclosureRef}
            onClick={(event) => onToggleMenu(event.currentTarget)}
          >
            Historique et détails
          </button>
          <div
            className="editorial-topbar__menu"
            id="editorial-topbar-history-actions"
            hidden={!menuOpen}
          >
            <button
              type="button"
              onClick={() => {
                const disclosure = historyDisclosureRef.current;
                if (disclosure) onOpenHistory?.(disclosure);
              }}
            >
              Historique
            </button>
            <button type="button" onClick={onOpenTechnicalDetails}>
              Détails techniques
            </button>
          </div>
        </div>
      </div>
      {pdfStatusUnavailable ? (
        <div className="editorial-pdf-status__error" role="alert">
          <p>{statusDescription}</p>
          {onRefreshPdfStatus ? (
            <button
              className="button button--secondary"
              type="button"
              onClick={onRefreshPdfStatus}
            >
              Actualiser le statut PDF
            </button>
          ) : null}
        </div>
      ) : currentPdfStatus === "FAILED" || pdfRegenerationError ? (
        <div className="editorial-pdf-status__error" role="alert">
          <p>{pdfErrorMessage}</p>
          <p>
            Corrigez le problème signalé, puis lancez explicitement un nouveau
            rendu.
          </p>
        </div>
      ) : null}
      {compositionStatus?.status === "stale" ? (
        <p className="editorial-composition-stale" role="status">
          Composition à mettre à jour
          <a
            href={`/editions/${encodeURIComponent(compositionStatus.edition_id)}/composition`}
          >
            Ouvrir la composition
          </a>
        </p>
      ) : null}
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
          {technicalDetailRows(snapshot, pdfStatus).map(([label, value]) => (
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
