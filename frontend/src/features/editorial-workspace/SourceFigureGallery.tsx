import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";

import {
  editorialFigureThumbnailUrl,
  getEditorialFigureCatalog,
  getEditorialFigurePreview,
  type EditorialFigureCatalogItem,
  type EditorialFigureStatus,
} from "../../api/editorialWorkspace";
import { editorialWorkspaceQueryKeys } from "./editorialWorkspaceQueries";

const STATUS_LABELS: Record<EditorialFigureStatus, string> = {
  available: "Disponible",
  already_integrated: "Déjà intégrée",
  not_selected: "Non retenue",
  ineligible: "Non éligible",
  file_unavailable: "Fichier indisponible",
  unverified: "Figure non vérifiée",
};

function sourceLabel(item: EditorialFigureCatalogItem): string {
  if (item.source.title) {
    return item.source.url
      ? `${item.source.title} · ${item.source.url}`
      : item.source.title;
  }
  return `Source ${item.source.source_id}`;
}

export function SourceFigureGallery({
  subjectId,
  onClose,
  selectedFigures,
  onSelectionChange,
  selectionLimit,
  initialFocusRef,
}: {
  subjectId: string;
  onClose: () => void;
  selectedFigures?: EditorialFigureCatalogItem[];
  onSelectionChange?: (items: EditorialFigureCatalogItem[]) => void;
  selectionLimit?: number;
  initialFocusRef?: RefObject<HTMLElement | null>;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const previewHeadingRef = useRef<HTMLHeadingElement>(null);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<EditorialFigureStatus | "">("");
  const [source, setSource] = useState("");
  const [localSelectedFigures, setLocalSelectedFigures] = useState<
    EditorialFigureCatalogItem[]
  >([]);
  const [previewFigureId, setPreviewFigureId] = useState<string | null>(null);
  const [failedThumbnails, setFailedThumbnails] = useState<Set<string>>(
    () => new Set(),
  );
  const [previewImageFailed, setPreviewImageFailed] = useState(false);

  useEffect(() => {
    if (previewFigureId) previewHeadingRef.current?.focus();
  }, [previewFigureId]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    openerRef.current =
      initialFocusRef?.current ??
      (document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    closeButtonRef.current?.focus();
    return () => {
      if (dialog.open && typeof dialog.close === "function") dialog.close();
      openerRef.current?.focus();
    };
  }, [initialFocusRef]);

  const catalogQuery = useInfiniteQuery({
    queryKey: editorialWorkspaceQueryKeys.figureCatalog(
      subjectId,
      status,
      source,
      search.trim(),
    ),
    queryFn: ({ pageParam }) =>
      getEditorialFigureCatalog(
        subjectId,
        {
          ...(status ? { status } : {}),
          ...(source ? { source } : {}),
          ...(search.trim() ? { q: search.trim() } : {}),
        },
        pageParam,
      ),
    initialPageParam: 0,
    getNextPageParam: (lastPage) =>
      lastPage.offset + lastPage.items.length < lastPage.total
        ? lastPage.offset + lastPage.limit
        : undefined,
  });
  const items = useMemo(
    () => catalogQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [catalogQuery.data],
  );
  const controlledSelection =
    selectedFigures !== undefined && onSelectionChange !== undefined;
  const currentSelection = controlledSelection
    ? selectedFigures
    : localSelectedFigures;
  const selectedIds = useMemo(
    () => new Set(currentSelection.map((item) => item.figure_id)),
    [currentSelection],
  );
  const firstPage = catalogQuery.data?.pages[0];
  const previewQuery = useQuery({
    queryKey: editorialWorkspaceQueryKeys.figurePreview(
      subjectId,
      previewFigureId ?? "",
    ),
    queryFn: () => getEditorialFigurePreview(subjectId, previewFigureId ?? ""),
    enabled: previewFigureId !== null,
  });

  useEffect(() => {
    const newlyIneligible = new Set(
      items.filter((item) => !item.selectable).map((item) => item.figure_id),
    );
    if (newlyIneligible.size === 0) return;
    const next = currentSelection.filter(
      (item) => !newlyIneligible.has(item.figure_id),
    );
    if (next.length === currentSelection.length) return;
    if (controlledSelection) onSelectionChange(next);
    else setLocalSelectedFigures(next);
  }, [controlledSelection, currentSelection, items, onSelectionChange]);

  useEffect(() => {
    setPreviewImageFailed(false);
  }, [previewFigureId]);

  const toggleSelected = (item: EditorialFigureCatalogItem) => {
    if (!item.selectable) return;
    const isSelected = selectedIds.has(item.figure_id);
    if (
      !isSelected &&
      selectionLimit !== undefined &&
      selectedIds.size >= selectionLimit
    ) {
      return;
    }
    const next = isSelected
      ? currentSelection.filter(
          (selectedItem) => selectedItem.figure_id !== item.figure_id,
        )
      : [...currentSelection, item];
    if (controlledSelection) onSelectionChange(next);
    else setLocalSelectedFigures(next);
  };

  const close = () => onClose();
  const preview = previewQuery.data;

  return (
    <dialog
      ref={dialogRef}
      className="editorial-figure-gallery"
      aria-labelledby="editorial-figure-gallery-heading"
      aria-modal="true"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") close();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <div className="editorial-figure-gallery__layout">
        <header className="editorial-figure-gallery__header">
          <div>
            <p className="editorial-figure-gallery__eyebrow">
              Sources archivées
            </p>
            <h2 id="editorial-figure-gallery-heading">
              Galerie d’images sources
            </h2>
          </div>
          <button
            ref={closeButtonRef}
            className="button button--secondary"
            type="button"
            onClick={close}
            aria-label="Fermer la galerie d’images sources"
          >
            Fermer
          </button>
        </header>

        <div className="editorial-figure-gallery__toolbar">
          <label>
            <span>Rechercher dans les figures</span>
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.currentTarget.value)}
              placeholder="Nom, légende, contexte…"
            />
          </label>
          <label>
            <span>Article/source</span>
            <select
              value={source}
              onChange={(event) => setSource(event.currentTarget.value)}
            >
              <option value="">Toutes les sources</option>
              {firstPage?.sources.map((item) => (
                <option key={item.source_id} value={item.source_id}>
                  {item.title ?? `Source ${item.source_id}`}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Statut</span>
            <select
              value={status}
              onChange={(event) =>
                setStatus(
                  event.currentTarget.value as EditorialFigureStatus | "",
                )
              }
            >
              <option value="">Tous les statuts</option>
              {Object.entries(STATUS_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <p className="editorial-figure-gallery__counts" aria-live="polite">
            <span>Disponibles : {firstPage?.available_count ?? 0}</span>
            <span>
              {selectedIds.size}{" "}
              {selectedIds.size === 1
                ? "image sélectionnée"
                : "images sélectionnées"}
            </span>
          </p>
        </div>

        {selectionLimit !== undefined ? (
          <p
            id="editorial-figure-selection-limit"
            className="editorial-figure-gallery__limit"
            role="note"
          >
            {selectionLimit === 0
              ? "Une brève ne peut contenir plus de trois figures intégrées. Le plafond est atteint."
              : `Cette insertion peut ajouter jusqu’à ${selectionLimit} figure${selectionLimit > 1 ? "s" : ""}.`}
          </p>
        ) : null}

        {catalogQuery.isPending ? (
          <p className="editorial-figure-gallery__state" role="status">
            Chargement de la galerie…
          </p>
        ) : catalogQuery.isError ? (
          <div className="editorial-figure-gallery__state" role="alert">
            <p>
              {catalogQuery.error.message ||
                "La galerie n’a pas pu être chargée."}
            </p>
            <button
              className="button button--secondary"
              type="button"
              onClick={() => void catalogQuery.refetch()}
            >
              Réessayer la lecture
            </button>
          </div>
        ) : items.length === 0 ? (
          <p className="editorial-figure-gallery__state" role="status">
            {search || status || source
              ? "Aucune figure ne correspond à ces filtres."
              : "Aucune figure disponible dans les sources archivées."}
          </p>
        ) : (
          <>
            <div
              className={`editorial-figure-gallery__content${previewFigureId ? " editorial-figure-gallery__content--with-preview" : ""}`}
            >
              <ul
                className="editorial-figure-gallery__grid"
                aria-label="Figures candidates"
              >
                {items.map((item) => {
                  const disabledByLimit =
                    selectionLimit !== undefined &&
                    selectedIds.size >= selectionLimit &&
                    !selectedIds.has(item.figure_id);
                  const descriptions = [
                    item.exclusion_reason
                      ? `figure-exclusion-${item.figure_id}`
                      : null,
                    disabledByLimit ? "editorial-figure-selection-limit" : null,
                  ].filter((value): value is string => value !== null);
                  return (
                    <li key={item.figure_id}>
                      <article className="editorial-figure-card">
                        <div className="editorial-figure-card__image">
                          {failedThumbnails.has(item.figure_id) ? (
                            <span role="status">Erreur de miniature</span>
                          ) : (
                            <img
                              src={editorialFigureThumbnailUrl(
                                subjectId,
                                item.figure_id,
                              )}
                              alt={item.alt_text ?? item.original_name}
                              loading="lazy"
                              onError={() =>
                                setFailedThumbnails((current) =>
                                  new Set(current).add(item.figure_id),
                                )
                              }
                            />
                          )}
                        </div>
                        <div className="editorial-figure-card__body">
                          <h3>{item.original_name}</h3>
                          {item.original_caption ? (
                            <p>{item.original_caption}</p>
                          ) : null}
                          <p className="editorial-figure-card__source">
                            {sourceLabel(item)}
                          </p>
                          <p
                            className={`editorial-figure-status editorial-figure-status--${item.status}`}
                          >
                            {STATUS_LABELS[item.status]}
                            {item.already_used ? " · Déjà utilisée" : ""}
                          </p>
                          {item.exclusion_reason ? (
                            <p
                              id={`figure-exclusion-${item.figure_id}`}
                              className="editorial-figure-card__reason"
                            >
                              {item.exclusion_reason}
                            </p>
                          ) : null}
                          <div className="editorial-figure-card__actions">
                            <label className="editorial-figure-card__checkbox">
                              <input
                                type="checkbox"
                                checked={selectedIds.has(item.figure_id)}
                                disabled={!item.selectable || disabledByLimit}
                                onChange={() => toggleSelected(item)}
                                aria-label={`Sélectionner ${item.original_name}`}
                                aria-describedby={
                                  descriptions.length > 0
                                    ? descriptions.join(" ")
                                    : undefined
                                }
                              />
                              <span>
                                {selectedIds.has(item.figure_id)
                                  ? "Sélectionnée"
                                  : "Sélectionner"}
                              </span>
                            </label>
                            <button
                              className="button button--secondary"
                              type="button"
                              onClick={() => setPreviewFigureId(item.figure_id)}
                            >
                              Aperçu détaillé
                            </button>
                          </div>
                        </div>
                      </article>
                    </li>
                  );
                })}
              </ul>
              {previewFigureId ? (
                <aside
                  className="editorial-figure-gallery__preview"
                  aria-labelledby="editorial-figure-preview-heading"
                  aria-live="polite"
                >
                  <div className="editorial-figure-gallery__preview-heading">
                    <h3
                      id="editorial-figure-preview-heading"
                      ref={previewHeadingRef}
                      tabIndex={-1}
                    >
                      Aperçu détaillé
                    </h3>
                    <button
                      className="button button--secondary"
                      type="button"
                      onClick={() => setPreviewFigureId(null)}
                    >
                      Fermer l’aperçu
                    </button>
                  </div>
                  {previewQuery.isPending ? (
                    <p role="status">Chargement de l’aperçu…</p>
                  ) : previewQuery.isError || !preview ? (
                    <p role="alert">L’aperçu détaillé n’est pas disponible.</p>
                  ) : (
                    <>
                      {previewImageFailed ? (
                        <p role="status">Erreur de miniature</p>
                      ) : (
                        <img
                          className="editorial-figure-gallery__preview-image"
                          src={preview.preview_image_url}
                          alt={preview.alt_text ?? preview.original_name}
                          onError={() => setPreviewImageFailed(true)}
                        />
                      )}
                      <h4>{preview.original_name}</h4>
                      {preview.original_caption ? (
                        <p>{preview.original_caption}</p>
                      ) : null}
                      <p>{sourceLabel(preview)}</p>
                      <p>
                        {preview.page ? `Page ${preview.page}` : null}
                        {preview.page && preview.section ? " · " : ""}
                        {preview.section ?? ""}
                      </p>
                      {preview.width && preview.height ? (
                        <p>
                          {preview.width} × {preview.height} px
                          {preview.mime_type ? ` · ${preview.mime_type}` : ""}
                        </p>
                      ) : null}
                      <p
                        className={`editorial-figure-status editorial-figure-status--${preview.status}`}
                      >
                        {STATUS_LABELS[preview.status]}
                      </p>
                      {preview.exclusion_reason ? (
                        <p>{preview.exclusion_reason}</p>
                      ) : null}
                      {preview.context_before ? (
                        <blockquote>{preview.context_before}</blockquote>
                      ) : null}
                      {preview.context_after ? (
                        <blockquote>{preview.context_after}</blockquote>
                      ) : null}
                    </>
                  )}
                </aside>
              ) : null}
            </div>
            {catalogQuery.hasNextPage ? (
              <button
                className="button button--secondary editorial-figure-gallery__more"
                type="button"
                disabled={catalogQuery.isFetchingNextPage}
                onClick={() => void catalogQuery.fetchNextPage()}
              >
                {catalogQuery.isFetchingNextPage
                  ? "Chargement…"
                  : "Afficher plus"}
              </button>
            ) : null}
          </>
        )}
      </div>
    </dialog>
  );
}
