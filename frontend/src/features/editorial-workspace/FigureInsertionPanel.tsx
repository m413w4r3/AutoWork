import { useMutation } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { ApiError } from "../../api/editions";
import {
  createManualEditorialSourceFigureProposal,
  type EditorialFigureCatalogItem,
  type EditorialWorkspaceBlock,
  type EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { EditorialProposalCard } from "./EditorialProposalCard";
import { SourceFigureGallery } from "./SourceFigureGallery";

interface FigureSelectionDraft {
  item: EditorialFigureCatalogItem;
  caption: string;
}

interface FigureInsertionPanelProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock;
  canCreateFigure: boolean;
  onClose: () => void;
  onRefresh: () => void;
}

const BRIEF_FIGURE_LIMIT_MESSAGE =
  "Une brève ne peut contenir plus de trois figures intégrées.";

const FIGURE_ERROR_MESSAGES: Record<string, string> = {
  editorial_anchor_stale:
    "Le document a changé. Actualisez l’atelier avant de préparer une nouvelle proposition.",
  editorial_figure_not_found:
    "Cette figure n’est plus disponible dans l’inventaire. Actualisez la galerie et choisissez une autre figure.",
  editorial_figure_not_eligible:
    "Cette figure ne peut pas être intégrée. Actualisez la galerie et choisissez une figure éligible.",
  figure_catalog_unavailable:
    "La galerie de figures est temporairement indisponible. Réessayez dans quelques instants.",
  manual_source_figure_request_invalid:
    "La sélection de figures est invalide. Vérifiez les légendes et l’ordre, puis réessayez.",
  proposal_idempotency_conflict:
    "Cette demande correspond à une autre sélection. Actualisez l’atelier avant de continuer.",
  source_figure_inventory_limit_exceeded:
    "Le nombre de figures dépasse la limite de l’inventaire archivé. Réduisez la sélection et réessayez.",
};

function figureError(error: Error): { message: string; refresh: boolean } {
  if (!(error instanceof ApiError)) {
    return {
      message:
        "Les images n’ont pas pu être intégrées. Vérifiez la connexion, puis réessayez avec la même sélection.",
      refresh: false,
    };
  }
  if (error.code === "brief_figure_limit_exceeded") {
    return { message: BRIEF_FIGURE_LIMIT_MESSAGE, refresh: true };
  }
  return {
    message:
      FIGURE_ERROR_MESSAGES[error.code] ??
      "Les images n’ont pas pu être intégrées. Vérifiez la sélection et réessayez.",
    refresh: error.status === 409,
  };
}

function defaultCaption(item: EditorialFigureCatalogItem): string {
  return item.original_caption?.trim() || item.original_name;
}

function integratedFigureCount(snapshot: EditorialWorkspaceSnapshot): number {
  return snapshot.blocks.filter(
    (block) => block.kind === "figure" && block.figure?.status === "included",
  ).length;
}

export function FigureInsertionPanel({
  subjectId,
  snapshot,
  block,
  canCreateFigure,
  onClose,
  onRefresh,
}: FigureInsertionPanelProps) {
  const [galleryOpen, setGalleryOpen] = useState(true);
  const [selectedFigures, setSelectedFigures] = useState<
    FigureSelectionDraft[]
  >([]);
  const [proposalId, setProposalId] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<{
    message: string;
    refresh: boolean;
  } | null>(null);
  const galleryButtonRef = useRef<HTMLButtonElement>(null);
  const proposalRequestRef = useRef<{
    idempotencyKey: string;
    signature: string;
  } | null>(null);
  const existingFigureCount = integratedFigureCount(snapshot);
  const selectionLimit =
    snapshot.publication_kind === "brief"
      ? Math.max(0, 3 - existingFigureCount)
      : undefined;

  const resetDraftFeedback = () => {
    proposalRequestRef.current = null;
    setProposalId(null);
    setRequestError(null);
  };

  const updateSelection = (items: EditorialFigureCatalogItem[]) => {
    setSelectedFigures((current) => {
      const previous = new Map(
        current.map((selection) => [selection.item.figure_id, selection]),
      );
      return items.map(
        (item) =>
          previous.get(item.figure_id) ?? {
            item,
            caption: defaultCaption(item),
          },
      );
    });
    resetDraftFeedback();
  };

  const proposalMutation = useMutation({
    mutationFn: () => {
      const figures = selectedFigures.map((selection, order) => ({
        figure_id: selection.item.figure_id,
        caption: selection.caption.trim(),
        order,
      }));
      const signature = JSON.stringify({
        snapshot_id: snapshot.snapshot_id,
        anchor_block_id: block.block_id,
        figures,
      });
      let request = proposalRequestRef.current;
      if (!request || request.signature !== signature) {
        request = { idempotencyKey: crypto.randomUUID(), signature };
        proposalRequestRef.current = request;
      }
      return createManualEditorialSourceFigureProposal(
        subjectId,
        block.block_id,
        request.idempotencyKey,
        {
          snapshot_id: snapshot.snapshot_id,
          anchor_block_id: block.block_id,
          figures,
        },
      );
    },
    onSuccess: (proposal) => {
      proposalRequestRef.current = null;
      setProposalId(proposal.id);
      setRequestError(null);
    },
    onError: (error: Error) => {
      if (
        error instanceof ApiError &&
        error.status >= 400 &&
        error.status < 500
      ) {
        proposalRequestRef.current = null;
      }
      setRequestError(figureError(error));
    },
  });

  const canSubmit =
    canCreateFigure &&
    selectedFigures.length > 0 &&
    selectedFigures.every((selection) => selection.caption.trim().length > 0) &&
    !proposalMutation.isPending;

  const updateCaption = (figureId: string, caption: string) => {
    setSelectedFigures((current) =>
      current.map((selection) =>
        selection.item.figure_id === figureId
          ? { ...selection, caption }
          : selection,
      ),
    );
    resetDraftFeedback();
  };

  const removeFigure = (figureId: string) => {
    setSelectedFigures((current) =>
      current.filter((selection) => selection.item.figure_id !== figureId),
    );
    resetDraftFeedback();
  };

  const moveFigure = (index: number, offset: -1 | 1) => {
    const destination = index + offset;
    if (destination < 0 || destination >= selectedFigures.length) return;
    setSelectedFigures((current) => {
      const next = [...current];
      const [moved] = next.splice(index, 1);
      if (!moved) return current;
      next.splice(destination, 0, moved);
      return next;
    });
    resetDraftFeedback();
  };

  return (
    <section
      className="editorial-figure-insertion"
      role="region"
      aria-label="Intégrer des images sources"
    >
      <header className="editorial-figure-insertion__header">
        <div>
          <h3>Intégrer des images sources</h3>
          <p>{selectedFigures.length} images sélectionnées</p>
        </div>
        <button
          className="button button--secondary"
          type="button"
          onClick={onClose}
        >
          Fermer l’insertion
        </button>
      </header>

      <button
        ref={galleryButtonRef}
        className="button button--secondary"
        type="button"
        aria-haspopup="dialog"
        onClick={() => setGalleryOpen(true)}
      >
        {selectedFigures.length > 0
          ? "Modifier la sélection dans la galerie"
          : "Choisir dans la galerie d’images sources"}
      </button>
      {galleryOpen ? (
        <SourceFigureGallery
          subjectId={subjectId}
          onClose={() => setGalleryOpen(false)}
          selectedFigures={selectedFigures.map((selection) => selection.item)}
          onSelectionChange={updateSelection}
          selectionLimit={selectionLimit}
          initialFocusRef={galleryButtonRef}
        />
      ) : null}

      {snapshot.publication_kind === "brief" ? (
        <p className="editorial-figure-insertion__limit" role="note">
          {BRIEF_FIGURE_LIMIT_MESSAGE} Figures intégrées : {existingFigureCount}
          /3.
        </p>
      ) : null}

      <section
        className="editorial-figure-insertion__selection"
        aria-labelledby="editorial-figure-selection-heading"
      >
        <h4 id="editorial-figure-selection-heading">
          Sélection de figures ({selectedFigures.length})
        </h4>
        {selectedFigures.length === 0 ? (
          <p>
            Aucune image sélectionnée. Choisissez des figures éligibles dans la
            galerie.
          </p>
        ) : (
          <ol>
            {selectedFigures.map(({ item, caption }, index) => (
              <li key={item.figure_id}>
                <div className="editorial-figure-insertion__figure-heading">
                  <span>{item.original_name}</span>
                  <div>
                    <button
                      className="button button--secondary"
                      type="button"
                      disabled={index === 0}
                      aria-label={`Monter ${item.original_name}`}
                      onClick={() => moveFigure(index, -1)}
                    >
                      Monter
                    </button>
                    <button
                      className="button button--secondary"
                      type="button"
                      disabled={index === selectedFigures.length - 1}
                      aria-label={`Descendre ${item.original_name}`}
                      onClick={() => moveFigure(index, 1)}
                    >
                      Descendre
                    </button>
                    <button
                      className="button button--secondary"
                      type="button"
                      aria-label={`Retirer ${item.original_name}`}
                      onClick={() => removeFigure(item.figure_id)}
                    >
                      Retirer
                    </button>
                  </div>
                </div>
                <label className="editorial-figure-insertion__caption">
                  <span>Légende éditoriale pour {item.original_name}</span>
                  <input
                    value={caption}
                    maxLength={500}
                    required
                    onChange={(event) =>
                      updateCaption(item.figure_id, event.currentTarget.value)
                    }
                  />
                </label>
                <p className="editorial-figure-insertion__source">
                  {item.source.title ?? `Source ${item.source.source_id}`}
                  {item.width && item.height
                    ? ` · ${item.width} × ${item.height} px`
                    : ""}
                </p>
              </li>
            ))}
          </ol>
        )}
        <p className="editorial-figure-insertion__placement">
          <strong>Emplacement · </strong>
          Après le paragraphe sélectionné : {block.text ?? "Paragraphe courant"}
        </p>
      </section>

      {requestError ? (
        <div className="editorial-figure-insertion__error" role="alert">
          <p>{requestError.message}</p>
          {requestError.refresh ? (
            <>
              <p>
                Actualisez l’atelier pour recharger le nombre de figures
                intégrées et la sélection autorisée.
              </p>
              <button
                className="button button--secondary"
                type="button"
                onClick={() => {
                  setRequestError(null);
                  onRefresh();
                }}
              >
                Actualiser l’atelier
              </button>
            </>
          ) : null}
        </div>
      ) : null}
      <button
        className="button editorial-figure-insertion__submit"
        type="button"
        disabled={!canSubmit}
        onClick={() => {
          if (!canSubmit) return;
          setProposalId(null);
          setRequestError(null);
          proposalMutation.mutate();
        }}
      >
        {proposalMutation.isPending
          ? "Préparation de la proposition…"
          : "Intégrer les images sélectionnées"}
      </button>
      {proposalId ? (
        <div className="editorial-figure-insertion__proposal">
          <EditorialProposalCard
            subjectId={subjectId}
            occurrenceId={block.block_id}
            proposalId={proposalId}
            anchorText={block.text ?? undefined}
          />
        </div>
      ) : null}
    </section>
  );
}
