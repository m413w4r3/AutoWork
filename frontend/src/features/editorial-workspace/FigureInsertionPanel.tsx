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
import { FigureSelectionPanel } from "./FigureSelectionPanel";
import { SourceFigureGallery } from "./SourceFigureGallery";
import {
  BRIEF_FIGURE_LIMIT,
  briefFigureCapacity,
  defaultFigureCaption,
  figureSelectionLabel,
  integratedFigureCount,
  type FigureSelectionDraft,
} from "./replaceFigureContract";

interface FigureInsertionPanelProps {
  subjectId: string;
  snapshot: EditorialWorkspaceSnapshot;
  block: EditorialWorkspaceBlock;
  canCreateFigure: boolean;
  onClose: () => void;
  onRefresh: () => void;
  initialSelectedFigures?: EditorialFigureCatalogItem[];
  initialGalleryOpen?: boolean;
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

export function FigureInsertionPanel({
  subjectId,
  snapshot,
  block,
  canCreateFigure,
  onClose,
  onRefresh,
  initialSelectedFigures,
  initialGalleryOpen = true,
}: FigureInsertionPanelProps) {
  const [galleryOpen, setGalleryOpen] = useState(initialGalleryOpen);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [selectedFigures, setSelectedFigures] = useState<
    FigureSelectionDraft[]
  >(() =>
    (initialSelectedFigures ?? []).map((item) => ({
      item,
      caption: defaultFigureCaption(item),
    })),
  );
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
  const remainingCapacity = briefFigureCapacity(snapshot);
  const capacityNote =
    snapshot.publication_kind === "brief"
      ? `Figures intégrées : ${existingFigureCount}/${BRIEF_FIGURE_LIMIT} · Capacité restante : ${remainingCapacity ?? 0} figure${(remainingCapacity ?? 0) > 1 ? "s" : ""} sur ${BRIEF_FIGURE_LIMIT}.`
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
            caption: defaultFigureCaption(item),
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

  const captionsComplete = selectedFigures.every(
    (selection) => selection.caption.trim().length > 0,
  );
  const overflowsCapacity =
    remainingCapacity !== null && selectedFigures.length > remainingCapacity;
  const canSubmit =
    canCreateFigure &&
    selectedFigures.length > 0 &&
    captionsComplete &&
    !overflowsCapacity &&
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
      className="editorial-visual-insertion__panel editorial-figure-insertion"
      role="region"
      aria-label="Intégrer des images sources"
    >
      <header className="editorial-figure-insertion__header">
        <div>
          <h3>Intégrer des images sources</h3>
          <p>{figureSelectionLabel(selectedFigures.length)}</p>
        </div>
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
      </header>
      {galleryOpen ? (
        <SourceFigureGallery
          subjectId={subjectId}
          onClose={() => setGalleryOpen(false)}
          selectedFigures={selectedFigures.map((selection) => selection.item)}
          onSelectionChange={updateSelection}
          selectionLimit={remainingCapacity ?? undefined}
          capacityNote={capacityNote}
          initialFocusRef={galleryButtonRef}
        />
      ) : null}

      <FigureSelectionPanel
        subjectId={subjectId}
        selections={selectedFigures}
        anchorText={block.text?.trim() || null}
        publicationKind={snapshot.publication_kind}
        existingFigureCount={existingFigureCount}
        remainingCapacity={remainingCapacity}
        previewOpen={previewOpen}
        pending={proposalMutation.isPending}
        onCaptionChange={updateCaption}
        onMove={moveFigure}
        onRemove={removeFigure}
        onTogglePreview={() => setPreviewOpen((open) => !open)}
        onCancel={onClose}
        onIntegrate={() => {
          if (!canSubmit) return;
          setProposalId(null);
          setRequestError(null);
          proposalMutation.mutate();
        }}
        canIntegrate={canSubmit}
      />

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
