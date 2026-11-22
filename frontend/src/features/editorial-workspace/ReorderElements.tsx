import { useEffect, useMemo, useRef, useState } from "react";

import type {
  EditorialEnrichmentElementRef,
  EditorialWorkspaceBlock,
} from "../../api/editorialWorkspace";
import {
  visualElementKindLabel,
  visualElementRefs,
  visualElementTitle,
} from "./visualElementOperations";

interface ReorderElementsProps {
  anchorBlockId: string;
  elements: readonly EditorialWorkspaceBlock[];
  saving: boolean;
  error: string | null;
  onSave: (order: EditorialEnrichmentElementRef[]) => void;
  onCancel: () => void;
}

function sameOrder(
  left: readonly EditorialEnrichmentElementRef[],
  right: readonly EditorialEnrichmentElementRef[],
): boolean {
  return (
    left.length === right.length &&
    left.every(
      (item, index) =>
        item.kind === right[index]?.kind && item.key === right[index]?.key,
    )
  );
}

function refKey(ref: EditorialEnrichmentElementRef): string {
  return `${ref.kind}:${ref.key}`;
}

/**
 * Ordered preview and Monter/Descendre controls for the elements sharing one
 * paragraph anchor. Saving sends the exact permutation of the group, never a
 * partial move that could be mistaken for a placement change.
 */
export function ReorderElements({
  anchorBlockId,
  elements,
  saving,
  error,
  onSave,
  onCancel,
}: ReorderElementsProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const originalOrder = useMemo(() => visualElementRefs(elements), [elements]);
  const [draftOrder, setDraftOrder] =
    useState<EditorialEnrichmentElementRef[]>(originalOrder);
  const byRef = useMemo(
    () =>
      new Map(
        elements.flatMap((block) => {
          const ref = visualElementRefs([block])[0];
          return ref ? [[refKey(ref), block] as const] : [];
        }),
      ),
    [elements],
  );

  useEffect(() => {
    setDraftOrder(originalOrder);
  }, [originalOrder]);

  useEffect(() => {
    panelRef.current?.focus({ preventScroll: true });
  }, []);

  const move = (index: number, delta: -1 | 1) => {
    const target = index + delta;
    if (target < 0 || target >= draftOrder.length) return;
    setDraftOrder((current) => {
      const next = [...current];
      const item = next[index];
      const other = next[target];
      if (!item || !other) return current;
      next[index] = other;
      next[target] = item;
      return next;
    });
  };

  const unchanged = sameOrder(draftOrder, originalOrder);
  const preview = draftOrder.map((ref, index) => {
    const block = byRef.get(refKey(ref));
    return `${index + 1}. ${block ? visualElementTitle(block) : ref.key}`;
  });

  return (
    <div
      ref={panelRef}
      className="visual-reorder"
      role="group"
      aria-label="Réorganiser les visuels"
      tabIndex={-1}
      data-anchor-block-id={anchorBlockId}
    >
      <h4>Ordre des visuels après ce paragraphe</h4>
      <p className="visual-reorder__help">
        Utilisez Monter ou Descendre, puis enregistrez l’ordre. Aucun
        déplacement d’emplacement n’est effectué.
      </p>
      <ol className="visual-reorder__list" aria-label="Ordre prévu">
        {draftOrder.map((ref, index) => {
          const block = byRef.get(refKey(ref));
          const title = block ? visualElementTitle(block) : ref.key;
          return (
            <li key={refKey(ref)} className="visual-reorder__item">
              <span className="visual-reorder__position" aria-hidden="true">
                {index + 1}
              </span>
              <span className="visual-reorder__title">
                {title}
                <span className="visual-reorder__kind">
                  {" "}
                  · {block ? visualElementKindLabel(block) : ref.kind}
                </span>
              </span>
              <span className="visual-reorder__controls">
                <button
                  className="button button--secondary"
                  type="button"
                  aria-label={`Monter ${title}`}
                  disabled={index === 0 || saving}
                  title={index === 0 ? "Déjà en première position." : undefined}
                  onClick={() => move(index, -1)}
                >
                  Monter
                </button>
                <button
                  className="button button--secondary"
                  type="button"
                  aria-label={`Descendre ${title}`}
                  disabled={index === draftOrder.length - 1 || saving}
                  title={
                    index === draftOrder.length - 1
                      ? "Déjà en dernière position."
                      : undefined
                  }
                  onClick={() => move(index, 1)}
                >
                  Descendre
                </button>
              </span>
              {index === 0 ? (
                <span className="visual-reorder__limit">
                  Déjà en première position
                </span>
              ) : index === draftOrder.length - 1 ? (
                <span className="visual-reorder__limit">
                  Déjà en dernière position
                </span>
              ) : null}
            </li>
          );
        })}
      </ol>
      <p className="visual-reorder__preview" aria-live="polite">
        Ordre prévu : {preview.join(" · ")}
      </p>
      {unchanged ? (
        <p className="visual-reorder__hint">
          L’ordre est identique à celui du document.
        </p>
      ) : null}
      {error ? (
        <p className="visual-reorder__error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="visual-reorder__actions">
        <button
          className="button"
          type="button"
          disabled={unchanged || saving}
          onClick={() => onSave(draftOrder)}
        >
          {saving ? "Enregistrement…" : "Enregistrer l’ordre"}
        </button>
        <button
          className="button button--secondary"
          type="button"
          disabled={saving}
          onClick={onCancel}
        >
          Annuler
        </button>
      </div>
      <p className="visual-reorder__status" role="status" aria-live="polite">
        {saving ? "Enregistrement de l’ordre en cours." : ""}
      </p>
    </div>
  );
}
