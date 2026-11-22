import { useEffect, useRef } from "react";
import type { KeyboardEvent } from "react";

import type { EditorialWorkspaceBlock } from "../../api/editorialWorkspace";
import {
  visualElementKindLabel,
  visualElementTitle,
} from "./visualElementOperations";

interface RemoveElementDialogProps {
  block: EditorialWorkspaceBlock;
  saving: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hidden);
}

/**
 * Modal confirmation for removing one visual element from the document. It
 * uses the same native `<dialog>` technique as the replace dialog and the
 * gallery; the archived source, its blob and the history are out of scope and
 * are named explicitly. No native confirm/alert is used.
 */
export function RemoveElementDialog({
  block,
  saving,
  error,
  onConfirm,
  onCancel,
}: RemoveElementDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    openerRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    cancelRef.current?.focus({ preventScroll: true });
    return () => {
      if (dialog.open && typeof dialog.close === "function") dialog.close();
      openerRef.current?.focus?.({ preventScroll: true });
    };
  }, []);

  const handleKeyDown = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (!saving) onCancel();
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = focusableElements(dialog);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="visual-dialog-scrim">
      <dialog
        ref={dialogRef}
        className="visual-remove-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="visual-remove-dialog-title"
        aria-describedby="visual-remove-dialog-description"
        onKeyDown={handleKeyDown}
        onCancel={(event) => {
          event.preventDefault();
          if (!saving) onCancel();
        }}
      >
        <h3 id="visual-remove-dialog-title">Retirer ce visuel du document ?</h3>
        <p id="visual-remove-dialog-description">
          Retirer ce visuel du document ? Il restera dans les sources archivées
          et dans l’historique.
        </p>
        <p className="visual-remove-dialog__target">
          {visualElementKindLabel(block)} · {visualElementTitle(block)}
        </p>
        {error ? (
          <p className="visual-remove-dialog__error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="visual-remove-dialog__actions">
          <button
            className="button visual-remove-dialog__confirm"
            type="button"
            disabled={saving}
            onClick={onConfirm}
          >
            {saving ? "Retrait en cours…" : "Retirer le visuel"}
          </button>
          <button
            ref={cancelRef}
            className="button button--secondary"
            type="button"
            disabled={saving}
            onClick={onCancel}
          >
            Annuler
          </button>
        </div>
        <p
          className="visual-remove-dialog__status"
          role="status"
          aria-live="polite"
        >
          {saving ? "Retrait du visuel en cours." : ""}
        </p>
      </dialog>
    </div>
  );
}
