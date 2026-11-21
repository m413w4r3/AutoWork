import { useEffect, useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent } from "react";

import { ApiError } from "../../api/editions";

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
  const [announcementIsError, setAnnouncementIsError] = useState(false);

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

    const block = editorRef.current?.closest<HTMLElement>(
      ".editorial-document__block",
    );
    setSaving(true);
    setAnnouncement("");
    setAnnouncementIsError(false);
    try {
      await onSave(blockId, value);
      window.requestAnimationFrame(() => block?.focus({ preventScroll: true }));
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "L’enregistrement a échoué.";
      const recovery =
        error instanceof ApiError && error.status === 409
          ? "Actualisez l’atelier avant de réessayer."
          : error instanceof ApiError && error.status === 503
            ? "Réessayez plus tard."
            : "";
      setAnnouncement(
        `${message}${recovery ? ` ${recovery}` : ""} Le brouillon local est conservé.`,
      );
      setAnnouncementIsError(true);
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
    if (saving) return;
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
    window.requestAnimationFrame(() => block?.focus({ preventScroll: true }));
  };

  const handleChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    setAnnouncement("");
    setAnnouncementIsError(false);
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
            {saving ? "Enregistrement…" : "Enregistrer"}
          </button>
        ) : null}
        <button
          className="button button--secondary"
          type="button"
          disabled={saving}
          onClick={cancel}
        >
          Annuler
        </button>
      </div>
      <p
        className="editorial-inline-editor__announcement"
        role={announcementIsError ? "alert" : "status"}
        aria-live={announcementIsError ? "assertive" : "polite"}
      >
        {saving ? "Enregistrement…" : announcement}
      </p>
    </div>
  );
}
