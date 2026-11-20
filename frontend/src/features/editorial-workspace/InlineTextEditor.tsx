import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent } from "react";

export interface InlineTextEditorProps {
  blockId: string;
  value: string;
  hasLocalDraft: boolean;
  staleDraft: boolean;
  onChange: (value: string) => void;
  onCancel: () => void;
  onSave?: (blockId: string, value: string) => void | Promise<void>;
}

export function InlineTextEditor({
  blockId,
  value,
  hasLocalDraft,
  staleDraft,
  onChange,
  onCancel,
  onSave,
}: InlineTextEditorProps) {
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const [saving, setSaving] = useState(false);
  const [announcement, setAnnouncement] = useState("");

  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) return;
    editor.focus();
    editor.setSelectionRange(editor.value.length, editor.value.length);
  }, []);

  const save = async () => {
    if (staleDraft) return;
    if (!onSave) {
      setAnnouncement(
        "L’enregistrement serveur sera disponible dans une prochaine étape. Le brouillon reste en mémoire.",
      );
      return;
    }
    if (saving) return;

    setSaving(true);
    setAnnouncement("");
    try {
      await onSave(blockId, value);
    } catch {
      setAnnouncement("L’enregistrement a échoué. Le brouillon est conservé.");
    } finally {
      setSaving(false);
    }
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
    if (
      hasLocalDraft &&
      !window.confirm("Abandonner le brouillon local de ce bloc ?")
    ) {
      return;
    }
    cancel();
  };

  const cancel = () => {
    const block = editorRef.current?.closest<HTMLElement>(
      ".editorial-document__block",
    );
    onCancel();
    window.requestAnimationFrame(() => block?.focus());
  };

  const handleChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    setAnnouncement("");
    onChange(event.currentTarget.value);
  };

  return (
    <div className="editorial-inline-editor">
      <label className="editorial-inline-editor__label">
        Modifier le texte du bloc
        <textarea
          ref={editorRef}
          aria-label="Texte du bloc"
          value={value}
          rows={6}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
        />
      </label>
      {staleDraft ? (
        <p
          className="editorial-local-draft-stale"
          role="status"
          aria-live="polite"
        >
          La version du document a changé depuis ce brouillon
        </p>
      ) : null}
      <div className="editorial-inline-editor__actions">
        {onSave ? (
          <button
            className="button"
            type="button"
            disabled={saving || staleDraft}
            onClick={() => void save()}
          >
            Enregistrer
          </button>
        ) : null}
        <button
          className="button button--secondary"
          type="button"
          onClick={cancel}
        >
          Annuler
        </button>
      </div>
      <p
        className="editorial-inline-editor__announcement"
        role="status"
        aria-live="polite"
      >
        {announcement}
      </p>
    </div>
  );
}
