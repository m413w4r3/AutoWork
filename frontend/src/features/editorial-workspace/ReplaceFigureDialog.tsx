import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";

import { ApiError } from "../../api/editions";
import {
  editorialFigureThumbnailUrl,
  type EditorialFigureCatalogItem,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";
import { prepareAndAcceptVisualRevision } from "./manualVisualRevision";
import { SourceFigureGallery } from "./SourceFigureGallery";
import {
  briefFigureCapacityNote,
  figureSourceLabel,
  replaceFigureErrorInfo,
  type ReplaceFigureDialogProps,
  type ReplaceFigureErrorInfo,
} from "./replaceFigureContract";
import {
  visualCapabilityFlags,
  visualMutationUnavailableReason,
} from "./visualElementOperations";

const ARCHIVE_UNTOUCHED_MESSAGE =
  "L’archive d’origine n’est pas modifiée : la figure remplacée reste conservée dans l’inventaire.";

function FigureImage({
  subjectId,
  figureId,
  alt,
}: {
  subjectId: string;
  figureId: string | null;
  alt: string;
}) {
  const [failed, setFailed] = useState(false);
  if (!figureId || failed) {
    return (
      <p className="editorial-gallery-replace__image-unavailable" role="status">
        Aperçu indisponible
      </p>
    );
  }
  return (
    <img
      className="editorial-gallery-replace__image"
      src={editorialFigureThumbnailUrl(subjectId, figureId)}
      alt={alt}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

export function ReplaceFigureDialog({
  subjectId,
  snapshot,
  target,
  onClose,
  onAccepted,
  onRefresh,
}: ReplaceFigureDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const headingId = useId();
  const captionId = useId();
  const idempotencyRef = useRef<{
    key: string;
    acceptKey: string;
    signature: string;
  } | null>(null);
  const [selected, setSelected] = useState<EditorialFigureCatalogItem | null>(
    null,
  );
  const [caption, setCaption] = useState(target.currentFigure.caption ?? "");
  const [error, setError] = useState<ReplaceFigureErrorInfo | null>(null);
  const [accepted, setAccepted] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const keepEditingRef = useRef<HTMLButtonElement>(null);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (confirmClose) keepEditingRef.current?.focus();
  }, [confirmClose]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    openerRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    closeButtonRef.current?.focus();
    return () => {
      if (dialog.open && typeof dialog.close === "function") dialog.close();
      const opener = openerRef.current;
      window.requestAnimationFrame(() => {
        if (opener?.isConnected) {
          opener.focus({ preventScroll: true });
          return;
        }
        document
          .querySelector<HTMLElement>(
            '.editorial-document__block[data-selected="true"]',
          )
          ?.focus({ preventScroll: true });
      });
    };
  }, []);

  const invalidateWorkspace = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: editorialWorkspaceQueryKeys.snapshot(subjectId),
      }),
      queryClient.invalidateQueries({
        queryKey: editorialWorkspaceQueryKeys.proposalsForOccurrence(
          subjectId,
          target.blockId,
        ),
      }),
      queryClient.invalidateQueries({
        queryKey: editorialWorkspaceQueryKeys.history(subjectId),
      }),
    ]);
  };

  const replacementMutation = useMutation({
    mutationFn: async () => {
      if (!selected) throw new Error("Aucune image sélectionnée.");
      const nextCaption = caption.trim();
      const changed =
        nextCaption !== (target.currentFigure.caption ?? "").trim();
      const revision = {
        snapshot_id: snapshot.snapshot_id,
        operation: "replace_source_figure" as const,
        element_key: target.elementKey,
        figure_id: selected.figure_id,
        ...(changed ? { caption: nextCaption } : {}),
      };
      const signature = JSON.stringify(revision);
      let request = idempotencyRef.current;
      if (!request || request.signature !== signature) {
        request = {
          key: crypto.randomUUID(),
          acceptKey: crypto.randomUUID(),
          signature,
        };
        idempotencyRef.current = request;
      }
      const outcome = await prepareAndAcceptVisualRevision(
        subjectId,
        target.blockId,
        revision,
        {
          prepareIdempotencyKey: request.key,
          acceptIdempotencyKey: request.acceptKey,
        },
      );
      return outcome.proposal;
    },
    onSuccess: async () => {
      idempotencyRef.current = null;
      setError(null);
      setAccepted(true);
      await invalidateWorkspace();
      onAccepted?.(target.elementKey);
    },
    onError: (mutationError: Error) => {
      if (
        mutationError instanceof ApiError &&
        mutationError.status >= 400 &&
        mutationError.status < 500
      ) {
        idempotencyRef.current = null;
      }
      setError(replaceFigureErrorInfo(mutationError));
    },
  });

  const captionInvalid = caption.trim().length === 0;
  const capacityNote = briefFigureCapacityNote(snapshot);
  const mutationReason = visualMutationUnavailableReason(
    visualCapabilityFlags(snapshot),
  );
  const canConfirm =
    selected !== null &&
    !captionInvalid &&
    !replacementMutation.isPending &&
    !accepted &&
    mutationReason === null;
  // Selecting a replacement or editing the caption commits nothing until the
  // explicit replace action; closing silently would discard that work.
  const discardableChanges =
    !accepted &&
    (selected !== null ||
      caption.trim() !== (target.currentFigure.caption ?? "").trim());
  const closeNow = () => onClose();
  const requestClose = () => {
    if (discardableChanges && !confirmClose) {
      setConfirmClose(true);
      return;
    }
    closeNow();
  };
  const dismissCloseConfirmation = () => setConfirmClose(false);

  const figureId = target.currentFigure.catalog_figure_id ?? null;

  return (
    <dialog
      ref={dialogRef}
      className="editorial-gallery-replace"
      aria-labelledby={headingId}
      aria-modal="true"
      onCancel={(event) => {
        event.preventDefault();
        if (confirmClose) dismissCloseConfirmation();
        else requestClose();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        if (confirmClose) dismissCloseConfirmation();
        else requestClose();
      }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        if (confirmClose) dismissCloseConfirmation();
        else requestClose();
      }}
    >
      <div className="editorial-gallery-replace__layout">
        <header className="editorial-gallery-replace__header">
          <div>
            <p className="editorial-gallery-replace__eyebrow">
              Opération versionnée
            </p>
            <h2 id={headingId}>Remplacer l’image intégrée</h2>
            <p className="editorial-gallery-replace__subtitle">
              Une seule image remplace l’image actuelle ; aucun autre contenu
              n’est modifié.
            </p>
          </div>
          <button
            ref={closeButtonRef}
            className="button button--secondary"
            type="button"
            onClick={requestClose}
            aria-label="Fermer le remplacement d’image"
          >
            Fermer
          </button>
        </header>

        {confirmClose ? (
          <div
            className="editorial-close-confirmation"
            role="alert"
            aria-label="Confirmer la fermeture du remplacement"
          >
            <p>
              Fermer le remplacement sans l’appliquer ? La sélection et la
              légende modifiée seront perdues.
            </p>
            <button
              ref={keepEditingRef}
              className="button"
              type="button"
              onClick={dismissCloseConfirmation}
            >
              Continuer le remplacement
            </button>
            <button
              className="button button--secondary"
              type="button"
              onClick={closeNow}
            >
              Fermer sans enregistrer
            </button>
          </div>
        ) : null}

        <div className="editorial-gallery-replace__body">
          <div className="editorial-gallery-replace__picker">
            <SourceFigureGallery
              subjectId={subjectId}
              variant="embedded"
              mode="single"
              heading="Choisir une image de remplacement"
              selectedFigures={selected ? [selected] : []}
              onSelectionChange={(items) => {
                setSelected(items[0] ?? null);
                setError(null);
              }}
              selectionLimit={1}
            />
          </div>

          <div className="editorial-gallery-replace__side">
            <section
              className="editorial-gallery-replace__comparison"
              role="region"
              aria-label="Comparaison des images"
            >
              <div className="editorial-gallery-replace__cards">
                <article className="editorial-gallery-replace__card">
                  <h3>Image actuelle</h3>
                  <FigureImage
                    subjectId={subjectId}
                    figureId={figureId}
                    alt={target.currentFigure.caption}
                  />
                  <p className="editorial-gallery-replace__caption-text">
                    {target.currentFigure.caption}
                  </p>
                  <p className="editorial-gallery-replace__source">
                    {target.currentFigure.source ?? "Source archivée d’origine"}
                  </p>
                  <p className="editorial-gallery-replace__provenance">
                    {target.currentFigure.provenance}
                  </p>
                  <p className="editorial-gallery-replace__dimensions">
                    {target.currentFigure.width && target.currentFigure.height
                      ? `${target.currentFigure.width} × ${target.currentFigure.height} px · ${target.currentFigure.mime_type}`
                      : target.currentFigure.mime_type}
                  </p>
                </article>
                <article className="editorial-gallery-replace__card">
                  <h3>Nouvelle image</h3>
                  {selected ? (
                    <>
                      <FigureImage
                        subjectId={subjectId}
                        figureId={selected.figure_id}
                        alt={selected.original_name}
                      />
                      <p className="editorial-gallery-replace__name">
                        {selected.original_name}
                      </p>
                      {selected.original_caption ? (
                        <p className="editorial-gallery-replace__caption-text">
                          {selected.original_caption}
                        </p>
                      ) : null}
                      <p className="editorial-gallery-replace__source">
                        {figureSourceLabel(selected)}
                      </p>
                      <p className="editorial-gallery-replace__dimensions">
                        {selected.width && selected.height
                          ? `${selected.width} × ${selected.height} px · ${selected.mime_type}`
                          : selected.mime_type}
                      </p>
                    </>
                  ) : (
                    <p className="editorial-gallery-replace__placeholder">
                      Choisissez une image éligible dans la galerie pour la
                      comparer à l’image actuelle.
                    </p>
                  )}
                </article>
              </div>

              <p className="editorial-gallery-replace__anchor">
                {target.anchorText
                  ? `Emplacement conservé · après « ${target.anchorText} »`
                  : "Emplacement conservé · paragraphe d’ancrage inchangé"}
              </p>
              {capacityNote ? (
                <p className="editorial-gallery-replace__capacity" role="note">
                  {capacityNote} Le remplacement conserve ce total.
                </p>
              ) : null}
              <p className="editorial-gallery-replace__archive-note">
                {ARCHIVE_UNTOUCHED_MESSAGE}
              </p>

              {selected ? (
                <>
                  <label
                    className="editorial-gallery-replace__caption"
                    htmlFor={captionId}
                  >
                    <span>Légende de la nouvelle image</span>
                  </label>
                  <input
                    id={captionId}
                    value={caption}
                    maxLength={500}
                    aria-invalid={captionInvalid}
                    aria-describedby={
                      captionInvalid ? `${captionId}-error` : undefined
                    }
                    onChange={(event) => {
                      setCaption(event.currentTarget.value);
                      setError(null);
                    }}
                  />
                  {captionInvalid ? (
                    <p
                      id={`${captionId}-error`}
                      className="editorial-gallery-replace__hint"
                    >
                      La légende doit contenir au moins un caractère.
                    </p>
                  ) : (
                    <p className="editorial-gallery-replace__hint">
                      Laissez la légende actuelle ou modifiez-la ; la légende
                      d’origine reste consultable dans l’archive.
                    </p>
                  )}

                  {error ? (
                    <div
                      className="editorial-gallery-replace__error"
                      role="alert"
                    >
                      <p>{error.message}</p>
                      {error.refresh && onRefresh ? (
                        <button
                          className="button button--secondary"
                          type="button"
                          onClick={() => {
                            setError(null);
                            onRefresh();
                          }}
                        >
                          Actualiser l’atelier
                        </button>
                      ) : null}
                    </div>
                  ) : null}

                  {accepted ? (
                    <>
                      <p
                        className="editorial-gallery-replace__success"
                        role="status"
                      >
                        Image remplacée. Le document contient toujours
                        exactement une image à cet emplacement.
                      </p>
                      <p className="editorial-gallery-replace__archive-note">
                        {ARCHIVE_UNTOUCHED_MESSAGE}
                      </p>
                    </>
                  ) : null}

                  {mutationReason ? (
                    <p className="editorial-gallery-replace__hint" role="note">
                      {mutationReason}
                    </p>
                  ) : null}

                  <div className="editorial-gallery-replace__actions">
                    <button
                      className="button"
                      type="button"
                      disabled={!canConfirm}
                      onClick={() => {
                        if (!canConfirm) return;
                        setError(null);
                        replacementMutation.mutate();
                      }}
                    >
                      {replacementMutation.isPending
                        ? "Remplacement en cours…"
                        : "Remplacer l’image"}
                    </button>
                  </div>
                </>
              ) : (
                <p className="editorial-gallery-replace__hint">
                  La légende et la confirmation apparaissent après le choix
                  d’une image de remplacement.
                </p>
              )}
            </section>
          </div>
        </div>
      </div>
    </dialog>
  );
}
