/**
 * Brief Draft Editor
 * Allows users to view and edit brief drafts before final assembly
 */

import { useQuery, useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { saveBriefDraft, getBriefDraft } from "../api/production";

interface BriefDraftEditorProps {
  subjectId: string;
  isAvailable: boolean;
  onClose?: () => void;
}

export function BriefDraftEditor({
  subjectId,
  isAvailable,
  onClose,
}: BriefDraftEditorProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editContent, setEditContent] = useState("");

  const {
    data: draft,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ["brief-draft", subjectId],
    queryFn: () => getBriefDraft(subjectId),
    enabled: isAvailable,
  });

  const saveMutation = useMutation({
    mutationFn: () => saveBriefDraft(subjectId, editContent),
    onSuccess: () => {
      setIsEditing(false);
      void refetch();
    },
  });

  const handleEdit = () => {
    setEditContent(draft?.content || "");
    setIsEditing(true);
  };

  if (!isAvailable) {
    return null;
  }

  if (isLoading) {
    return (
      <section className="brief-draft-panel">
        <h3>Brouillon de la brève</h3>
        <p>Chargement du brouillon…</p>
      </section>
    );
  }

  if (error && !draft) {
    return null; // No draft yet, that's ok
  }

  return (
    <section className="brief-draft-panel">
      <div className="brief-draft-header">
        <div>
          <h3>Brouillon de la brève</h3>
          {draft && (
            <p className="brief-draft-meta">
              Version {draft.draft_version} • Sauvegardé le{" "}
              {new Date(draft.saved_at).toLocaleString()}
            </p>
          )}
        </div>
        {onClose && (
          <button className="button button--secondary" onClick={onClose}>
            Fermer
          </button>
        )}
      </div>

      {isEditing ? (
        <div className="brief-draft-editor">
          <textarea
            className="brief-draft-textarea"
            rows={20}
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            placeholder="Contenu du brouillon de la brève…"
          />
          {saveMutation.error && (
            <p className="error-message" role="alert">
              Erreur lors de la sauvegarde : {String(saveMutation.error)}
            </p>
          )}
          <div className="brief-draft-actions">
            <button
              className="button"
              disabled={saveMutation.isPending || !editContent.trim()}
              onClick={() => saveMutation.mutate()}
            >
              {saveMutation.isPending ? "Sauvegarde…" : "Sauvegarder"}
            </button>
            <button
              className="button button--secondary"
              disabled={saveMutation.isPending}
              onClick={() => setIsEditing(false)}
            >
              Annuler
            </button>
          </div>
        </div>
      ) : draft ? (
        <div className="brief-draft-view">
          <div className="brief-draft-content">
            <pre>{draft.content}</pre>
          </div>
          <div className="brief-draft-actions">
            <button className="button" onClick={handleEdit}>
              Éditer le brouillon
            </button>
          </div>
        </div>
      ) : (
        <div className="brief-draft-empty">
          <p>Aucun brouillon sauvegardé pour le moment.</p>
          <button className="button" onClick={handleEdit}>
            Créer un brouillon
          </button>
        </div>
      )}
    </section>
  );
}
