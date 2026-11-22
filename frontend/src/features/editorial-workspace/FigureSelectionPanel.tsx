import {
  editorialFigureThumbnailUrl,
  type EditorialPublicationKind,
} from "../../api/editorialWorkspace";
import {
  figureSelectionLabel,
  figureSourceLabel,
  type FigureSelectionDraft,
} from "./replaceFigureContract";

export interface FigureSelectionPanelProps {
  subjectId: string;
  selections: FigureSelectionDraft[];
  anchorText: string | null;
  publicationKind: EditorialPublicationKind | null;
  existingFigureCount: number;
  remainingCapacity: number | null;
  previewOpen: boolean;
  pending: boolean;
  onCaptionChange: (figureId: string, caption: string) => void;
  onMove: (index: number, offset: -1 | 1) => void;
  onRemove: (figureId: string) => void;
  onTogglePreview: () => void;
  onCancel: () => void;
  onIntegrate: () => void;
  canIntegrate: boolean;
}

function anchorLabel(anchorText: string | null): string {
  return anchorText
    ? `après « ${anchorText} »`
    : "après le paragraphe sélectionné";
}

export function FigureSelectionPanel({
  subjectId,
  selections,
  anchorText,
  publicationKind,
  existingFigureCount,
  remainingCapacity,
  previewOpen,
  pending,
  onCaptionChange,
  onMove,
  onRemove,
  onTogglePreview,
  onCancel,
  onIntegrate,
  canIntegrate,
}: FigureSelectionPanelProps) {
  const selectionOverflows =
    remainingCapacity !== null && selections.length > remainingCapacity;
  return (
    <section
      className="gallery-selection"
      aria-label="Sélection des images à intégrer"
    >
      <header className="gallery-selection__header">
        <div>
          <h3>Images sélectionnées</h3>
          <p className="gallery-selection__count">
            {figureSelectionLabel(selections.length)}
          </p>
        </div>
      </header>

      {publicationKind === "brief" ? (
        <p className="gallery-selection__limit" role="note">
          Figures intégrées : {existingFigureCount}/3 · Capacité restante :{" "}
          {remainingCapacity ?? 0} figure
          {(remainingCapacity ?? 0) > 1 ? "s" : ""} sur 3.
          {remainingCapacity === 0
            ? " Le plafond de trois figures est atteint : aucune image supplémentaire ne peut être intégrée."
            : null}
          {selectionOverflows
            ? " La sélection dépasse la capacité restante de la brève. Retirez des images avant d’intégrer."
            : null}
        </p>
      ) : null}

      {selections.length === 0 ? (
        <p className="gallery-selection__empty">
          Aucune image sélectionnée. Choisissez des figures éligibles dans la
          galerie.
        </p>
      ) : (
        <ol
          className="gallery-selection__list"
          aria-label="Images sélectionnées dans l’ordre d’insertion"
        >
          {selections.map(({ item, caption }, index) => (
            <li className="gallery-selection__item" key={item.figure_id}>
              <div className="gallery-selection__media">
                <img
                  src={editorialFigureThumbnailUrl(subjectId, item.figure_id)}
                  alt=""
                  loading="lazy"
                />
                <span
                  className="gallery-selection__order"
                  aria-label={`Position ${index + 1} dans l’ordre d’insertion`}
                >
                  {index + 1}
                </span>
              </div>
              <div className="gallery-selection__body">
                <div className="gallery-selection__heading">
                  <span className="gallery-selection__name">
                    {item.original_name}
                  </span>
                  <div className="gallery-selection__item-actions">
                    <button
                      className="button button--secondary"
                      type="button"
                      disabled={index === 0}
                      aria-label={`Monter ${item.original_name}`}
                      onClick={() => onMove(index, -1)}
                    >
                      Monter
                    </button>
                    <button
                      className="button button--secondary"
                      type="button"
                      disabled={index === selections.length - 1}
                      aria-label={`Descendre ${item.original_name}`}
                      onClick={() => onMove(index, 1)}
                    >
                      Descendre
                    </button>
                    <button
                      className="button button--secondary"
                      type="button"
                      aria-label={`Retirer ${item.original_name}`}
                      onClick={() => onRemove(item.figure_id)}
                    >
                      Retirer
                    </button>
                  </div>
                </div>
                <label className="gallery-selection__caption">
                  <span>Légende éditoriale pour {item.original_name}</span>
                  <input
                    value={caption}
                    maxLength={500}
                    required
                    onChange={(event) =>
                      onCaptionChange(item.figure_id, event.currentTarget.value)
                    }
                  />
                </label>
                {item.original_caption ? (
                  <p className="gallery-selection__original">
                    Légende d’origine · {item.original_caption}
                  </p>
                ) : null}
                <p className="gallery-selection__source">
                  {figureSourceLabel(item)}
                  {item.width && item.height
                    ? ` · ${item.width} × ${item.height} px`
                    : ""}
                </p>
                <p className="gallery-selection__target">
                  Emplacement · {anchorLabel(anchorText)}
                </p>
              </div>
            </li>
          ))}
        </ol>
      )}

      {previewOpen ? (
        <section
          className="gallery-selection__preview"
          role="region"
          aria-label="Prévisualisation dans le document"
        >
          <h4>Prévisualisation dans le document</h4>
          <p className="gallery-selection__preview-target">
            {anchorText
              ? `Après « ${anchorText} »`
              : "Après le paragraphe sélectionné"}
          </p>
          {selections.length === 0 ? (
            <p>Aucune image à prévisualiser.</p>
          ) : (
            <ol>
              {selections.map(({ item, caption }) => (
                <li key={`preview-${item.figure_id}`}>
                  <figure>
                    <img
                      src={editorialFigureThumbnailUrl(
                        subjectId,
                        item.figure_id,
                      )}
                      alt={caption}
                      loading="lazy"
                    />
                    <figcaption>{caption}</figcaption>
                  </figure>
                  <p>{figureSourceLabel(item)}</p>
                </li>
              ))}
            </ol>
          )}
          <p className="gallery-selection__preview-note">
            Aperçu local, non enregistré : l’intégration reste à confirmer.
          </p>
        </section>
      ) : null}

      <div className="gallery-selection__actions">
        <button
          className="button button--secondary"
          type="button"
          onClick={onCancel}
        >
          Annuler
        </button>
        <button
          className="button button--secondary"
          type="button"
          onClick={onTogglePreview}
        >
          Prévisualiser dans le document
        </button>
        <button
          className="button"
          type="button"
          disabled={!canIntegrate}
          onClick={onIntegrate}
        >
          {pending
            ? "Préparation de la proposition…"
            : "Intégrer les images sélectionnées"}
        </button>
      </div>
    </section>
  );
}
