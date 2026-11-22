import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent } from "react";

import type { EditorialWorkspaceBlock } from "../../api/editorialWorkspace";
import { visualElementTitle } from "./visualElementOperations";

interface CaptionEditorProps {
  block: EditorialWorkspaceBlock;
  onSave: (caption: string | null) => Promise<void>;
  onCancel: () => void;
}

function normalizedCaption(value: string): string | null {
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/**
 * Inline editor for the editorial caption of one visual element. The archived
 * original caption is shown read-only and is never part of the saved payload.
 */
export function CaptionEditor({ block, onSave, onCancel }: CaptionEditorProps) {
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const initialCaption = block.caption ?? "";
  const [value, setValue] = useState(initialCaption);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const originalCaption = block.figure?.original_caption ?? null;
  const dirty = value !== initialCaption;
  const unchanged =
    normalizedCaption(value) === normalizedCaption(initialCaption);

  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) return;
    editor.focus();
    editor.setSelectionRange(editor.value.length, editor.value.length);
  }, []);

  const save = async () => {
    if (saving || unchanged) return;
    setSaving(true);
    setError(null);
    try {
      await onSave(normalizedCaption(value));
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : "L’enregistrement de la légende a échoué.",
      );
      setSaving(false);
      return;
    }
    setSaving(false);
  };

  const requestCancel = () => {
    if (saving) return;
    if (dirty) {
      setConfirmingCancel(true);
      return;
    }
    onCancel();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      void save();
      return;
    }
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    if (confirmingCancel) {
      setConfirmingCancel(false);
      editorRef.current?.focus({ preventScroll: true });
      return;
    }
    requestCancel();
  };

  const handleChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    setError(null);
    setValue(event.currentTarget.value);
  };

  return (
    <section
      className="visual-caption-editor"
      aria-labelledby={`visual-caption-editor-${block.block_id}`}
    >
      <h4 id={`visual-caption-editor-${block.block_id}`}>
        Légende du visuel · {visualElementTitle(block)}
      </h4>
      <div className="visual-caption-editor__original">
        <p className="visual-caption-editor__label">Légende d’origine</p>
        <p className="visual-caption-editor__original-text">
          {originalCaption ??
            "Aucune légende d’origine archivée pour ce visuel."}
        </p>
      </div>
      <label className="visual-caption-editor__label">
        Légende éditoriale
        <textarea
          ref={editorRef}
          aria-label="Légende éditoriale"
          rows={3}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          readOnly={saving}
        />
      </label>
      <div className="visual-caption-editor__preview" aria-live="polite">
        <p className="visual-caption-editor__label">Aperçu</p>
        <p className="visual-caption-editor__preview-text">
          {normalizedCaption(value) ?? "Aucune légende"}
        </p>
      </div>
      {unchanged ? (
        <p className="visual-caption-editor__hint">
          La légende est inchangée. Modifiez-la pour activer l’enregistrement.
        </p>
      ) : null}
      {confirmingCancel ? (
        <div className="visual-caption-editor__confirm" role="alert">
          <p>Abandonner la modification de la légende ?</p>
          <div className="visual-caption-editor__actions">
            <button
              className="button"
              type="button"
              onClick={() => {
                setConfirmingCancel(false);
                onCancel();
              }}
            >
              Abandonner la modification
            </button>
            <button
              className="button button--secondary"
              type="button"
              onClick={() => {
                setConfirmingCancel(false);
                editorRef.current?.focus({ preventScroll: true });
              }}
            >
              Continuer l’édition
            </button>
          </div>
        </div>
      ) : (
        <div className="visual-caption-editor__actions">
          <button
            className="button"
            type="button"
            disabled={saving || unchanged}
            onClick={() => void save()}
          >
            {saving ? "Enregistrement…" : "Enregistrer"}
          </button>
          <button
            className="button button--secondary"
            type="button"
            disabled={saving}
            onClick={requestCancel}
          >
            Annuler
          </button>
        </div>
      )}
      <p
        className="visual-caption-editor__status"
        role="status"
        aria-live="polite"
      >
        {error ? (
          <strong role="alert" aria-live="assertive">
            {error}
          </strong>
        ) : saving ? (
          "Enregistrement de la légende…"
        ) : null}
      </p>
      <p className="visual-caption-editor__shortcuts">
        Ctrl/Cmd+S enregistre, Échap annule. La légende d’origine reste
        inchangée.
      </p>
    </section>
  );
}
